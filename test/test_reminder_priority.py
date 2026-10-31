"""Reminder (schedule-kind) jobs: dispatch and delivery priority with fencing intact."""
import threading
import unittest
from concurrent.futures import Future
from unittest.mock import Mock

from openbridge.adapters.persistence.sqlite import JobStore
from openbridge.domain.messages import ConversationKey
from openbridge.runtime.dispatcher import ConversationDispatcher
from openbridge.runtime.jobs import DurableRuntime

KEY = ConversationKey("telegram", "1234", "42")


def enqueue(store, kind, source_key):
    with store.transaction():
        job_id, created = store._enqueue_trigger(KEY, kind, source_key, {"kind": kind})
    assert created
    store.acknowledge(job_id)
    return job_id


def finish_definitely(store, job_id, outputs):
    assert store.claim(job_id)
    assert store.finish(job_id, outputs)


class TestReminderDispatch(unittest.TestCase):
    def setUp(self):
        self.store = JobStore()
        self.addCleanup(self.store.close)

    def test_earliest_reminder_is_dispatched_first(self):
        enqueue(self.store, "initiative", "initiative-1")
        reminder = enqueue(self.store, "schedule", "2026-10-31 09:00")
        pending = [job["id"] for job in self.store.pending(lane="background")]
        self.assertEqual(pending[0], reminder, "reminder outranks an earlier backlog id")

    def test_reminder_bypasses_a_running_leader_but_nothing_else(self):
        leader = enqueue(self.store, "initiative", "leader-1")
        self.assertTrue(self.store.claim(leader))
        reminder = enqueue(self.store, "schedule", "occurrence")
        # Dispatcher key must differ so a long task never serializes reminders.
        captured = []
        runtime = DurableRuntime(self.store, Mock(), lambda job, payload: None, Mock(), Mock(),
                                 background_dispatcher=Mock(
                                     submit=lambda key, function, *args: (
                                         captured.append((key, args)), finished)[1]))
        finished = Future()
        finished.set_result(None)
        runtime._pump_lane("background")
        self.assertEqual([(key, args) for key, args in captured], [(("reminder", KEY), (reminder,))],
                         "a running task leader must not hide the reminder")

    def test_reminder_serializes_though_dispatched_in_parallel(self):
        first = enqueue(self.store, "schedule", "a")
        second = enqueue(self.store, "schedule", "b")
        captured = []
        finished = Future()
        finished.set_result(None)
        recorder = Mock(submit=lambda key, function, *args:
                        (captured.append(key), finished)[1])
        runtime = DurableRuntime(self.store, Mock(), lambda job, payload: None, Mock(), Mock(),
                                 background_dispatcher=recorder)
        runtime._pump_lane("background")
        self.assertEqual(len({key for key in captured}), 1, "same-chat reminders keep their own FIFO")

    def test_non_schedule_jobs_keep_conversation_leadership(self):
        leader = enqueue(self.store, "initiative", "leader-1")
        self.assertTrue(self.store.claim(leader))
        follower = enqueue(self.store, "initiative", "follower-1")
        self.assertEqual([job["id"] for job in self.store.pending(lane="background")],
                         [], "queued work behind a leader stays hidden")
        self.assertIsNone(self.store.claim(follower))

    def test_interrupted_fence_blocks_reminders_too(self):
        leader = enqueue(self.store, "initiative", "leader-1")
        self.assertTrue(self.store.claim(leader))
        self.store.finish(leader, [], "interrupted")
        reminder = enqueue(self.store, "schedule", "occurrence")
        self.assertEqual(self.store.pending(lane="background"), [], "fence hides the reminder")
        self.assertIsNone(self.store.claim(reminder))


class TestReminderDelivery(unittest.TestCase):
    def setUp(self):
        self.store = JobStore()
        self.addCleanup(self.store.close)

    def test_reminder_output_delivers_before_pending_task_backlog(self):
        task = enqueue(self.store, "initiative", "leader-1")
        finish_definitely(self.store, task, [{"operation": "send", "text": "long progress report"}])
        reminder = enqueue(self.store, "schedule", "occurrence")
        finish_definitely(self.store, reminder, [{"operation": "send", "text": "⏰ reminder"}])
        first = self.store.claim_delivery()
        self.assertIn("reminder", first["payload"]["text"], "reminder jumps the background delivery queue")
        second = self.store.claim_delivery()
        self.assertIn("progress report", second["payload"]["text"])

    def test_interactive_acks_still_precede_reminders(self):
        interactive = self.store.accept(KEY, 5, {}, enqueue=True)[0]
        self.store.acknowledge(interactive, 7)
        finish_definitely(self.store, interactive, [{"operation": "edit", "message_id": 7,
                                                    "text": "Job acknowledged."}])
        reminder = enqueue(self.store, "schedule", "occurrence")
        finish_definitely(self.store, reminder, [{"operation": "send", "text": "⏰ reminder"}])
        first = self.store.claim_delivery()
        self.assertEqual(first["payload"]["operation"], "edit")


class TestReminderConcurrency(unittest.TestCase):
    """End-to-end: idle background workers can serve a reminder while a task stalls."""

    def test_reminder_completes_while_a_task_is_running(self):
        store = JobStore()
        self.addCleanup(store.close)
        gate = threading.Event()
        states = {"leader-released": False}

        def execute(job, payload):
            if job["kind"] != "schedule":
                gate.wait(5)
                states["leader-released"] = True

        runtime = DurableRuntime(store, Mock(), execute, Mock(), Mock(),
                                 background_dispatcher=ConversationDispatcher(max_workers=2))
        task = enqueue(store, "initiative", "leader-1")
        reminder = enqueue(store, "schedule", "occurrence")
        task_future = runtime.background_dispatcher.submit(("background", KEY), runtime.run, task)
        reminder_future = runtime.background_dispatcher.submit(("reminder", KEY), runtime.run, reminder)
        self.assertIsNone(reminder_future.result(timeout=10), "reminder completed without blocking")
        self.assertEqual(store.get(reminder)["status"], "succeeded", "reminder finished without waiting for the leader")
        self.assertFalse(task_future.done(), "task still holds its worker")
        gate.set()
        task_future.result(timeout=10)
        self.assertTrue(states["leader-released"])


if __name__ == "__main__":
    unittest.main()
