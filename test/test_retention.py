"""Conservative settled-job retention across the durable runtime tables."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from openbridge.adapters.persistence.sqlite import JobStore
from openbridge.bootstrap import create_application
from openbridge.config import Configuration, ConfigurationError, Paths, WorkerSettings
from openbridge.domain.messages import ConversationKey
from test.support import make_application


def settled_job(store, source_id, *, age=40 * 86400, delivery_status="sent", bot="1234"):
    """Real durable flow: accept → claim → finish → optional delivery outcome."""
    key = ConversationKey("telegram", bot, "42")
    job_id, created = store.accept(key, source_id, {}, enqueue=True)
    assert created, "test isolation: each store must use unique source_ids"
    store.acknowledge(job_id, 5)
    assert store.claim(job_id)
    assert store.finish(job_id, [{"operation": "send", "text": "done"}])
    with store.transaction():
        store.db.execute("UPDATE runtime_jobs SET updated_at=updated_at-? WHERE id=?", (age, job_id))
        store.db.execute("UPDATE runtime_deliveries SET created_at=created_at-?, next_try=0 WHERE job_id=?", (age, job_id))
        if delivery_status:
            store.db.execute("UPDATE runtime_deliveries SET status=? WHERE job_id=?", (delivery_status, job_id))
    return job_id


class TestSettledJobRetention(unittest.TestCase):
    def setUp(self):
        self.store = JobStore()
        self.addCleanup(self.store.close)

    def prune(self, **options):
        pruned = self.store.prune_terminal(**options)
        with self.store.transaction():
            dangling = self.store.db.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(dangling, [], "pruning must leave no dangling references")
        return pruned

    def count(self, table):
        return self.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_old_settled_job_with_final_delivery_is_pruned_completely(self):
        job_id = settled_job(self.store, 1, delivery_status="sent")
        self.assertEqual(self.prune(), [job_id])
        for table in ("runtime_jobs", "runtime_inbox", "runtime_runs",
                      "runtime_steps", "runtime_deliveries", "runtime_triggers"):
            self.assertEqual(self.count(table), 0, table)
        self.assertEqual(self.count("runtime_cursors"), 1, "ingress cursors are never pruned")

    def test_old_failed_and_cancelled_jobs_are_pruned_too(self):
        failed = settled_job(self.store, 1, delivery_status="dead", age=50 * 86400)
        # Real operator path to cancelled: an interrupted running job reconciled.
        cancelled = settled_job(self.store, 2, delivery_status="dead", age=50 * 86400)
        with self.store.transaction():
            self.store.db.execute("UPDATE runtime_jobs SET status='running' WHERE id=?", (cancelled,))
            self.store.db.execute("UPDATE runtime_jobs SET status='interrupted' WHERE id=?", (cancelled,))
        self.assertTrue(self.store.resolve(cancelled))
        # Reconciliation refreshes the job's age; after settling long enough it
        # becomes prunable like any other terminal job.
        with self.store.transaction():
            self.store.db.execute("UPDATE runtime_jobs SET updated_at=updated_at-? WHERE id=?", (50 * 86400, cancelled))
        self.assertEqual(sorted(self.prune()), sorted([failed, cancelled]))

    def test_recent_settled_jobs_are_retained(self):
        job_id = settled_job(self.store, 1, age=10 * 86400, delivery_status="sent")
        self.assertEqual(self.prune(), [])
        self.assertEqual(self.store.get(job_id)["id"], job_id)

    def test_batch_limit_bounds_the_work_and_oldest_go_first(self):
        oldest = settled_job(self.store, 1, age=90 * 86400, delivery_status="sent")
        middle = settled_job(self.store, 2, age=60 * 86400, delivery_status="sent")
        newest = settled_job(self.store, 3, age=30 * 86400, delivery_status="sent")
        pruned = self.prune(limit=2)
        self.assertEqual(sorted(pruned), sorted([oldest, middle]), "oldest settle first")
        self.assertIsNone(self.store.get(oldest))
        self.assertIsNone(self.store.get(middle))
        self.assertEqual(self.store.get(newest)["id"], newest)

    def test_interrupted_queued_and_running_jobs_are_never_pruned(self):
        interrupted = settled_job(self.store, 1, age=180 * 86400, delivery_status="sent")
        with self.store.transaction():
            self.store.db.execute("UPDATE runtime_jobs SET status='interrupted' WHERE id=?", (interrupted,))
        key = ConversationKey("telegram", "1234", "42")
        queued, created = self.store.accept(key, 2, {}, enqueue=True)
        assert created
        self.store.acknowledge(queued)
        with self.store.transaction():
            self.store.db.execute("UPDATE runtime_jobs SET updated_at=updated_at-? WHERE id=?", (180 * 86400, queued))
        running, created = self.store.accept(ConversationKey("telegram", "1234", "43"), 3, {}, enqueue=True)
        assert created
        self.store.acknowledge(running)
        self.assertTrue(self.store.claim(running))
        with self.store.transaction():
            self.store.db.execute("UPDATE runtime_jobs SET updated_at=updated_at-? WHERE id=?", (180 * 86400, running))
        self.assertEqual(self.prune(), [])
        self.assertEqual(self.store.get(interrupted)["status"], "interrupted")
        self.assertEqual(self.store.get(queued)["status"], "queued")
        self.assertEqual(self.store.get(running)["status"], "running")

    def test_unsettled_deliveries_block_pruning_until_final(self):
        # Ambiguous send quarantined by design: never pruned on age alone.
        uncertain = settled_job(self.store, 1, delivery_status="uncertain", age=180 * 86400)
        pending = settled_job(self.store, 2, delivery_status="pending", age=180 * 86400)
        self.assertEqual(self.prune(), [])
        self.assertIsNotNone(self.store.get(uncertain))
        self.assertIsNotNone(self.store.get(pending))
        # Once the outcomes become final, the same aged jobs become prunable.
        with self.store.transaction():
            self.store.db.execute("UPDATE runtime_deliveries SET status='sent' WHERE job_id IN (?,?)",
                                  (uncertain, pending))
        self.assertEqual(set(self.prune()), {uncertain, pending})

    def test_dead_delivery_is_final_and_prunable_alone(self):
        job_id = settled_job(self.store, 1, delivery_status="dead", age=60 * 86400)
        self.assertEqual(self.prune(), [job_id])


class TestRetentionConfiguration(unittest.TestCase):
    def validate(self, *, days=None):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            data = {"owner_ids": [42]}
            if days is not None:
                data["retention"] = {"days": days}
            configuration = Configuration(Paths(home, home / "missing"), data, WorkerSettings())
            try:
                create_application(configuration, backend=Mock(), channel_call=Mock())
            except ConfigurationError as error:
                return error
        return None

    def test_invalid_retention_days_are_rejected_before_any_state_is_opened(self):
        for days in (0, -7, 366, 30.5, "ten", "30", True):
            with self.subTest(days=days):
                self.assertIsNotNone(self.validate(days=days))

    def test_omitted_days_default_to_30_without_error(self):
        self.assertIsNone(self.validate(days=None))

    def test_integer_days_in_range_are_accepted(self):
        self.assertIsNone(self.validate(days=7))
        self.assertIsNone(self.validate(days=365))


if __name__ == "__main__":
    unittest.main()
