"""Queued-job cancellation: durable, owner-scoped, never touching running work."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from openbridge.adapters.persistence.sqlite.jobs import JobStore
from openbridge.domain.messages import ConversationKey
from openbridge.services.job_commands import JobCommands

KEY = ConversationKey("telegram", "999", "777")


def make_store():
    return JobStore(str(Path(tempfile.mkdtemp()) / "jobs.db"))


class StoreCancellation(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.job_id, _ = self.store.accept(KEY, 100, {"text": "hi"})

    def test_cancel_moves_queued_job_to_cancelled(self):
        self.assertTrue(self.store.cancel_queued(self.job_id))
        self.assertEqual(self.store.get(self.job_id)["status"], "cancelled")

    def test_cancel_is_idempotent_and_unknown_positive(self):
        self.assertTrue(self.store.cancel_queued(self.job_id))
        self.assertFalse(self.store.cancel_queued(self.job_id))
        self.assertFalse(self.store.cancel_queued(424242))

    def test_cancelled_jobs_never_reached_by_the_pump(self):
        self.assertTrue(self.store.cancel_queued(self.job_id))
        self.assertEqual(self.store.pending(), [])

    def test_running_jobs_cannot_be_cancelled_here(self):
        self.store.acknowledge(self.job_id)
        job = self.store.claim(self.job_id)
        self.assertIsNotNone(job)  # became running
        self.assertFalse(self.store.cancel_queued(self.job_id))
        self.assertEqual(self.store.get(self.job_id)["status"], "running")
        self.store.finish(self.job_id, [])  # still a normal running job
        self.assertEqual(self.store.get(self.job_id)["status"], "succeeded")


class CommandCancellation(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.send = Mock()
        self.backend = Mock()
        self.commands = JobCommands(self.store, self.backend, self.send, channel="telegram")

    def test_cancel_command_is_registered(self):
        self.assertIn("/cancel", JobCommands.commands)

    def test_cancel_command_cancels_queued_job_in_conversation(self):
        job_id, _ = self.store.accept(KEY, 101, {"text": "hi"})
        self.commands.handle("999", 777, f"/cancel {job_id}")
        self.assertEqual(self.store.get(job_id)["status"], "cancelled")
        self.assertIn("cancelled", self.send.call_args[0][0])

    def test_cancel_command_is_conversation_scoped(self):
        job_id, _ = self.store.accept(KEY, 101, {"text": "hi"})
        self.commands.handle("999", 888, f"/cancel {job_id}")
        self.assertEqual(self.store.get(job_id)["status"], "queued")
        self.assertIn("not found", self.send.call_args[0][0])

    def test_cancel_command_rejects_running_jobs(self):
        job_id, _ = self.store.accept(KEY, 102, {"text": "hi"})
        self.store.acknowledge(job_id)
        self.store.claim(job_id)
        self.commands.handle("999", 777, f"/cancel {job_id}")
        self.assertEqual(self.store.get(job_id)["status"], "running")
        self.assertIn("already running", self.send.call_args[0][0])

    def test_job_detail_offers_cancel_only_for_queued_jobs(self):
        job_id, _ = self.store.accept(KEY, 103, {"text": "hi"})
        self.commands.handle("999", 777, f"/job {job_id}")
        options = self.send.call_args[1]
        self.assertEqual(options.get("buttons"), [("Cancel", f"jx-{job_id}")])
        self.commands.handle("999", 777, f"/cancel {job_id}")
        self.commands.handle("999", 777, f"/job {job_id}")
        self.assertIsNone(self.send.call_args[1].get("buttons"))


if __name__ == "__main__":
    unittest.main()
