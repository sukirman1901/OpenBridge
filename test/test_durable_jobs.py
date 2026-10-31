import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from openbridge.adapters.persistence.sqlite import JobStore
from openbridge.domain.messages import ConversationKey
from openbridge.runtime.dispatcher import ConversationDispatcher
from openbridge.runtime.jobs import DurableRuntime, ExecutionUncertain


class TestJobStore(unittest.TestCase):
    def setUp(self):
        self.store = JobStore(clock=lambda: 100)
        self.addCleanup(self.store.close)
        self.key = ConversationKey("telegram", "123", "42")

    def accept(self, source=1, key=None, ack=99):
        job_id, created = self.store.accept(key or self.key, source, {"text": "hello"})
        self.assertTrue(created)
        self.store.acknowledge(job_id, ack)
        return job_id

    def test_acceptance_is_deduplicated_and_cursor_is_monotonic(self):
        job_id = self.accept(5)
        again, created = self.store.accept(self.key, 5, {"text": "changed"})
        self.assertFalse(created)
        self.assertEqual(again, job_id)
        self.assertEqual(json.loads(self.store.get(job_id)["payload"]), {"text": "hello"})
        self.accept(3)
        self.assertEqual(self.store.cursor("telegram", "123"), 6)

    def test_transaction_failure_does_not_advance_cursor_or_keep_inbox(self):
        self.store.db.execute("CREATE TRIGGER reject_jobs BEFORE INSERT ON runtime_jobs "
                              "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.accept(self.key, 1, {})
        self.assertEqual(self.store.cursor("telegram", "123"), 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM runtime_inbox").fetchone()[0], 0)

    def test_unauthorized_or_control_event_does_not_create_a_job(self):
        self.assertEqual(self.store.accept(self.key, 1, {}, enqueue=False), (None, True))
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.cursor("telegram", "123"), 2)

    def test_ack_and_fifo_gate_claiming(self):
        a, _ = self.store.accept(self.key, 1, {})
        b = self.accept(2)
        self.assertIsNone(self.store.claim(a))
        self.assertIsNone(self.store.claim(b))
        self.store.acknowledge(a, 10)
        self.assertIsNotNone(self.store.claim(a))
        self.assertIsNone(self.store.claim(b))
        self.store.finish(a, [])
        self.assertIsNotNone(self.store.claim(b))

    def test_result_and_outbox_commit_together_and_only_once(self):
        job_id = self.accept()
        self.store.claim(job_id)
        self.assertTrue(self.store.finish(job_id, [{"operation": "send", "text": "result"}]))
        self.assertFalse(self.store.finish(job_id, [{"operation": "send", "text": "duplicate"}]))
        self.assertEqual(self.store.get(job_id)["status"], "succeeded")
        delivery = self.store.claim_delivery()
        self.assertEqual(delivery["payload"], {"operation": "edit", "message_id": 99, "text": "result"})
        self.assertIsNone(self.store.claim_delivery())

    def test_outbox_failure_rolls_back_completion(self):
        job_id = self.accept()
        self.store.claim(job_id)
        self.store.db.execute("CREATE TRIGGER reject_delivery BEFORE INSERT ON runtime_deliveries "
                              "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.finish(job_id, [{"operation": "send", "text": "result"}])
        self.assertEqual(self.store.get(job_id)["status"], "running")
        self.assertIsNone(self.store.get(job_id)["result"])
        self.assertEqual(self.store.db.execute("SELECT status FROM runtime_runs").fetchone()[0], "running")

    def test_busy_conversation_does_not_fill_the_dispatch_window(self):
        first = self.accept()
        for source in range(2, 140):
            self.accept(source)
        other = self.accept(140, ConversationKey("telegram", "456", "42"))
        self.assertEqual([job["id"] for job in self.store.pending(limit=2)], [first, other])

    def test_restart_keeps_queued_but_does_not_replay_running_work(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "runtime.db")
            first = JobStore(path)
            a, _ = first.accept(self.key, 1, {})
            b, _ = first.accept(self.key, 2, {})
            first.acknowledge(a, 99)
            first.claim(a)
            first.checkpoint(a, "ses-old")
            first.close()
            second = JobStore(path)
            try:
                recovered = second.recover()
                self.assertEqual([job["id"] for job in recovered], [a])
                self.assertEqual(second.get(a)["status"], "interrupted")
                self.assertEqual(second.get(a)["session_id"], "ses-old")
                self.assertEqual(second.get(b)["status"], "queued")
                self.assertTrue(second.is_blocked(self.key))
                self.assertEqual(second.pending(), [])
                self.assertTrue(second.resolve(a))
                self.assertEqual([job["id"] for job in second.pending()], [b])
                self.assertIsNotNone(second.claim(b))
                # Recovery also queues a notification, without replaying work.
                self.assertEqual(second.claim_delivery()["payload"]["operation"], "edit")
            finally:
                second.close()

    def test_interrupted_conversation_does_not_block_another_account(self):
        job_id = self.accept()
        self.store.claim(job_id)
        self.store.finish(job_id, [], "interrupted")
        other = self.accept(2, ConversationKey("telegram", "456", "42"))
        self.assertEqual([job["id"] for job in self.store.pending()], [other])

    def test_recovery_retries_edits_but_quarantines_ambiguous_sends(self):
        a = self.accept(1)
        b = self.accept(2, ConversationKey("telegram", "123", "43"), ack=None)
        for job_id in (a, b):
            self.store.claim(job_id)
            self.store.finish(job_id, [{"operation": "send", "text": "result"}])
        edit = self.store.claim_delivery()
        send = self.store.claim_delivery()
        self.assertEqual(edit["payload"]["operation"], "edit")
        self.assertEqual(send["payload"]["operation"], "send")
        self.store.recover()
        rows = list(self.store.db.execute("SELECT status FROM runtime_deliveries ORDER BY id"))
        self.assertEqual([row[0] for row in rows], ["pending", "uncertain"])

    def test_delivery_chunks_cannot_overtake_an_uncertain_chunk(self):
        job_id = self.accept(ack=None)
        self.store.claim(job_id)
        self.store.finish(job_id, [{"operation": "send", "text": text} for text in ("first", "second")])
        first = self.store.claim_delivery()
        self.store.delivery_result(first, "uncertain")
        self.assertIsNone(self.store.claim_delivery())

    def test_migration_preserves_existing_tables_and_can_be_reopened(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "db")
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE legacy(value TEXT)")
                db.execute("INSERT INTO legacy VALUES('keep')")
            for _ in range(2):
                store = JobStore(path)
                self.assertEqual(store.db.execute("SELECT value FROM legacy").fetchone()[0], "keep")
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM runtime_migrations").fetchone()[0], 4)
                store.close()


class TestDurableRuntime(unittest.TestCase):
    def setUp(self):
        self.store = JobStore()
        self.dispatcher = ConversationDispatcher()
        self.addCleanup(self.store.close)
        self.addCleanup(self.dispatcher.shutdown, 3)
        self.key = ConversationKey("telegram", "123", "42")

    def accept(self, source=1):
        job_id, _ = self.store.accept(self.key, source, {})
        self.store.acknowledge(job_id, 99)
        return job_id

    def test_failed_delivery_does_not_reexecute_the_model(self):
        def execute(job, payload):
            runtime.checkpoint("session")
            runtime.emit({"operation": "send", "text": "result"})
        execute = Mock(side_effect=execute)
        runtime = DurableRuntime(self.store, self.dispatcher, execute,
                                 Mock(return_value=("pending", "offline", 1)), Mock())
        job_id = self.accept()
        runtime.run(job_id)
        runtime.deliver_one()
        runtime.run(job_id)
        execute.assert_called_once()
        self.assertEqual(self.store.get(job_id)["status"], "succeeded")
        self.assertEqual(self.store.get(job_id)["session_id"], "session")

    def test_uncertain_execution_fences_later_jobs(self):
        runtime = DurableRuntime(self.store, self.dispatcher,
                                 Mock(side_effect=ExecutionUncertain()), Mock(), Mock())
        first = self.accept(1)
        second = self.accept(2)
        runtime.run(first)
        runtime.run(second)
        self.assertEqual(self.store.get(first)["status"], "interrupted")
        self.assertEqual(self.store.get(second)["status"], "queued")

    def test_queued_work_survives_memory_queue_saturation(self):
        saturated = Mock()
        from openbridge.runtime.dispatcher import QueueFull
        saturated.submit.side_effect = QueueFull()
        execute = Mock()
        runtime = DurableRuntime(self.store, saturated, execute, Mock(), Mock())
        job_id = self.accept()
        runtime.pump()
        self.assertEqual(self.store.get(job_id)["status"], "queued")
        execute.assert_not_called()
