"""Reserved chat capacity and lane-aware durable ordering, without real models."""
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock

from store import Store
from task_queue import TaskQueue
from openbridge.adapters.persistence.sqlite import JobStore
from openbridge.adapters.persistence.sqlite.jobs import MIGRATION_1, MIGRATION_2
from openbridge.domain.jobs import execution_lane
from openbridge.domain.messages import ConversationKey
from openbridge.runtime.dispatcher import ConversationDispatcher
from openbridge.runtime.jobs import DurableRuntime


class TestExecutionLanes(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "state.db")
        legacy = Store(path)
        self.addCleanup(legacy.close)
        self.tasks = TaskQueue(path)
        self.addCleanup(self.tasks.close)
        self.store = JobStore(path, clock=lambda: 100)
        self.addCleanup(self.store.close)
        self.chat = ConversationDispatcher(max_workers=1)
        self.background = ConversationDispatcher(max_workers=1)
        self.addCleanup(self.chat.shutdown, 3)
        self.addCleanup(self.background.shutdown, 3)
        self.key = ConversationKey("telegram", "123", "42")

    def task(self, conversation=42):
        task_id = self.tasks.add_task("123", conversation, "background task")
        return self.store.enqueue_task(task_id, "123")

    def message(self, source=1):
        job_id, _ = self.store.accept(self.key, source, {})
        self.store.acknowledge(job_id)
        return job_id

    def test_chat_executes_while_same_conversation_background_is_still_running(self):
        background_id = self.task()
        message_id = self.message()
        background_started, chat_answered, release = (threading.Event() for _ in range(3))
        self.addCleanup(release.set)

        def execute(job, payload):
            if job["lane"] == "background":
                runtime.checkpoint("background-session")
                background_started.set()
                self.assertTrue(release.wait(3))
            else:
                self.assertTrue(background_started.wait(2))
                runtime.checkpoint("chat-session")
                runtime.emit({"operation": "send", "text": "chat answered"})
                chat_answered.set()

        runtime = DurableRuntime(self.store, self.chat, execute, Mock(), Mock(), self.background)
        # First occupy the background pool, then submit the newer chat turn.
        runtime._pump_lane("background")
        self.assertTrue(background_started.wait(2))
        runtime.pump()
        self.assertTrue(chat_answered.wait(2), "Chat was blocked by the background task")
        self.assertEqual(self.store.get(background_id)["status"], "running")
        self.assertEqual(self.store.get(background_id)["session_id"], "background-session")
        self.assertEqual(self.store.get(message_id)["session_id"], "chat-session")
        release.set()
        self.assertTrue(self.chat.shutdown(3))
        self.assertTrue(self.background.shutdown(3))
        self.assertEqual(self.store.get(message_id)["status"], "succeeded")

    def test_same_lane_stays_fifo_but_opposite_lane_can_claim(self):
        background_a, background_b = self.task(), self.task()
        chat_a, chat_b = self.message(1), self.message(2)
        self.assertIsNotNone(self.store.claim(background_a))
        self.assertIsNone(self.store.claim(background_b))
        self.assertIsNotNone(self.store.claim(chat_a))
        self.assertIsNone(self.store.claim(chat_b))
        self.store.finish(chat_a, [])
        self.assertIsNotNone(self.store.claim(chat_b))
        self.assertEqual(self.store.get(background_a)["status"], "running")

    def test_deferred_background_does_not_block_chat(self):
        task_id, later_task = self.task(), self.task()
        message_id = self.message()
        self.store.claim(task_id)
        self.store.defer(task_id, 15)
        self.assertEqual(self.store.pending(lane="background"), [])
        self.assertIsNone(self.store.claim(later_task))
        self.assertIsNotNone(self.store.claim(message_id))

    def test_uncertain_background_still_fences_both_lanes(self):
        background_id = self.task()
        message_id = self.message()
        self.store.claim(background_id)
        self.store.finish(background_id, [], "interrupted")
        self.assertEqual(self.store.pending(lane="interactive"), [])
        self.assertEqual(self.store.pending(lane="background"), [])
        self.assertIsNone(self.store.claim(message_id))
        self.store.resolve(background_id)
        self.assertIsNotNone(self.store.claim(message_id))

    def test_uncertain_chat_fences_background_too(self):
        message_id = self.message()
        background_id = self.task()
        self.store.claim(message_id)
        self.store.finish(message_id, [], "interrupted")
        self.assertIsNone(self.store.claim(background_id))

    def test_background_backlog_cannot_hide_chat_beyond_query_limit(self):
        for conversation in range(200, 340):
            self.task(conversation)
        message_id = self.message()
        self.assertEqual([job["id"] for job in self.store.pending(limit=1, lane="interactive")], [message_id])
        self.assertEqual(len(self.store.pending(limit=1, lane="background")), 1)

    def test_delivery_prefers_chat_without_reordering_background_chunks(self):
        background_id = self.task()
        self.store.claim(background_id)
        self.store.finish(background_id, [{"operation": "send", "text": text} for text in ("a", "b")])
        chat_id = self.message()
        self.store.claim(chat_id)
        self.store.finish(chat_id, [{"operation": "send", "text": "chat"}])
        chat = self.store.claim_delivery()
        self.assertEqual(chat["job_id"], chat_id)
        self.store.delivery_result(chat, "sent")
        first = self.store.claim_delivery()
        self.assertEqual(first["job_id"], background_id)
        self.assertEqual(first["payload"]["text"], "a")
        self.assertIsNone(self.store.claim_delivery())
        self.store.delivery_result(first, "sent")
        self.assertEqual(self.store.claim_delivery()["payload"]["text"], "b")

    def test_unknown_lane_and_job_kind_fail_closed(self):
        with self.assertRaises(ValueError):
            self.store.pending(lane="unknown")
        with self.assertRaises(ValueError):
            execution_lane("unknown")
        self.assertEqual(execution_lane("telegram_update"), "interactive")
        for kind in ("task", "schedule", "initiative"):
            self.assertEqual(execution_lane(kind), "background")

    def test_bridge_reserves_two_distinct_worker_pools(self):
        from test.support import make_application
        app = make_application(self)
        self.assertIsNot(app.worker_pools["interactive"], app.worker_pools["background"])
        self.assertIs(app.runtime.dispatcher, app.worker_pools["interactive"])
        self.assertIs(app.runtime.background_dispatcher, app.worker_pools["background"])

    def test_shutdown_does_not_claim_queued_work_in_either_lane(self):
        background_id, chat_id = self.task(), self.message()
        execute = Mock()
        runtime = DurableRuntime(self.store, self.chat, execute, Mock(), Mock(), self.background)
        runtime.stop()
        runtime.run(chat_id)
        runtime.run(background_id)
        execute.assert_not_called()
        self.assertEqual(self.store.get(chat_id)["status"], "queued")
        self.assertEqual(self.store.get(background_id)["status"], "queued")


class TestLaneMigration(unittest.TestCase):
    def test_upgrade_preserves_states_and_assigns_existing_jobs_to_lanes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.db")
            with sqlite3.connect(path) as db:
                db.executescript(MIGRATION_1 + MIGRATION_2)
                db.execute("CREATE TABLE runtime_migrations(version INTEGER PRIMARY KEY,applied_at REAL)")
                db.executemany("INSERT INTO runtime_migrations VALUES(?,0)", [(1,), (2,)])
                db.execute("INSERT INTO runtime_cursors VALUES('telegram','123',9)")
                for index, (kind, status) in enumerate((("telegram_update", "queued"), ("task", "running"),
                                                       ("schedule", "interrupted"), ("initiative", "succeeded")), 1):
                    db.execute("INSERT INTO runtime_inbox VALUES(?,'telegram','123',?,'{}',0)", (index, index))
                    db.execute("INSERT INTO runtime_jobs(id,inbox_id,channel,account_id,conversation_id,status,kind,created_at,updated_at) "
                               "VALUES(?,?,'telegram','123','42',?,?,0,0)", (index, index, status, kind))
            store = JobStore(path)
            try:
                self.assertEqual(store.cursor("telegram", "123"), 9)
                self.assertEqual([(store.get(index)["lane"], store.get(index)["status"]) for index in range(1, 5)],
                    [("interactive", "queued"), ("background", "running"), ("background", "interrupted"), ("background", "succeeded")])
            finally:
                store.close()
