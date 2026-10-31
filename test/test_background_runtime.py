import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock
from unittest.mock import patch

from store import Store
from task_queue import TaskQueue
from scheduler import matches
from openbridge.adapters.persistence.sqlite import JobStore
from openbridge.adapters.persistence.sqlite.jobs import MIGRATION_1
from openbridge.domain.messages import ConversationKey
from openbridge.domain.errors import RetryLater, ExecutionUncertain
from openbridge.runtime.dispatcher import ConversationDispatcher
from openbridge.runtime.jobs import DurableRuntime
from openbridge.services.triggers import BackgroundProducer, schedule_occurrence
from openbridge.services.background import BackgroundExecutor


class BackgroundFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "state.db")
        self.legacy = Store(self.path)
        self.tasks = TaskQueue(self.path)
        self.now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc).timestamp()
        self.jobs = JobStore(self.path, clock=lambda: self.now)
        self.addCleanup(self.legacy.close)
        self.addCleanup(self.tasks.close)
        self.addCleanup(self.jobs.close)
        self.producer = BackgroundProducer(self.jobs, self.legacy, matches)
        self.key = ConversationKey("telegram", "123", "42")

    def schedule(self, expression="19:00", kind="cron", account="123"):
        return self.legacy.add_schedule(account, 42, expression, kind, "send a greeting")

    def task_job(self, maximum=3):
        task_id = self.tasks.add_task("123", 42, "produce verified work", max_steps=maximum)
        return task_id, self.jobs.enqueue_task(task_id, "123")


class TestBackgroundProducers(BackgroundFixture):
    def test_schedule_mark_and_job_are_atomic_and_deduplicated(self):
        schedule_id = self.schedule()
        self.assertEqual(len(self.producer.schedules_due({"123"}, self.now)), 1)
        self.assertEqual(self.producer.schedules_due({"123"}, self.now), [])
        job = self.jobs.get(1)
        self.assertEqual(job["kind"], "schedule")
        self.assertEqual(json.loads(job["payload"])["schedule_id"], schedule_id)
        self.assertTrue(self.legacy.list_schedules()[0]["last_fired"])
        self.assertEqual(self.jobs.cursor("telegram", "123"), 0)

    def test_failed_enqueue_never_consumes_schedule_occurrence(self):
        self.schedule()
        self.jobs.db.execute("CREATE TRIGGER reject_schedule_job BEFORE INSERT ON runtime_jobs "
                             "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.producer.schedules_due({"123"}, self.now)
        self.assertIsNone(self.legacy.list_schedules()[0]["last_fired"])
        self.assertEqual(self.jobs.db.execute("SELECT COUNT(*) FROM runtime_triggers").fetchone()[0], 0)

    def test_stale_schedule_snapshot_is_not_accepted(self):
        schedule_id = self.schedule()
        row = self.legacy.list_schedules()[0]
        occurrence = schedule_occurrence(row, self.now, matches)
        self.legacy.mark_fired(schedule_id, "already changed")
        self.assertIsNone(self.jobs.enqueue_schedule(row, *occurrence))

    def test_cron_uses_wib_without_host_local_fallback(self):
        self.schedule("12:00")  # UTC 12:00 is WIB 19:00, not noon.
        self.assertEqual(self.producer.schedules_due({"123"}, self.now), [])

    def test_overdue_one_shot_is_accepted_once(self):
        expression = datetime.fromtimestamp(self.now - 60).isoformat(sep=" ")
        self.schedule(expression, "once")
        self.assertEqual(len(self.producer.schedules_due({"123"}, self.now)), 1)
        self.assertEqual(self.producer.schedules_due({"123"}, self.now + 3600), [])

    def test_interval_fires_once_per_due_occurrence(self):
        self.schedule("1m", "interval")
        self.assertEqual(len(self.producer.schedules_due({"123"}, self.now)), 1)
        self.assertEqual(self.producer.schedules_due({"123"}, self.now + 59), [])
        self.now += 60
        self.assertEqual(len(self.producer.schedules_due({"123"}, self.now)), 1)

    def test_invalid_expressions_and_unknown_accounts_do_not_create_jobs(self):
        self.schedule("nan", "interval")
        self.schedule("not-a-date", "once")
        self.schedule("bad cron")
        self.schedule(account="456")
        self.assertEqual(self.producer.schedules_due({"123"}, self.now), [])

    def test_distinct_schedules_in_same_minute_are_not_merged(self):
        self.schedule()
        self.schedule()
        self.assertEqual(len(set(self.producer.schedules_due({"123"}, self.now))), 2)

    def test_duplicate_entries_in_one_legacy_import_create_one_schedule(self):
        from openbridge.services.schedules import Schedules
        entry = {"bot_id": "123", "chat_id": 42, "jam": "19:00", "prompt": "greet"}
        service = Schedules(Path(self.path).parent, self.legacy,
                            {"cron": [entry, entry]}, lambda: [{"token": "123:test"}], Mock())
        service.sync()
        self.assertEqual(len(self.legacy.list_schedules()), 1)

    def test_task_acceptance_is_idempotent_and_does_not_claim_work_early(self):
        task_id, job_id = self.task_job()
        self.assertEqual(self.jobs.enqueue_task(task_id, "123"), job_id)
        self.assertEqual(self.tasks.get(task_id)["status"], "queued")
        self.jobs.claim(job_id)
        self.assertEqual(self.tasks.get(task_id)["status"], "running")
        self.assertEqual(self.producer.tasks_due({"123"}), [])

    def test_unknown_task_owner_is_not_enqueued(self):
        task_id = self.tasks.add_task("456", 42, "private work")
        self.assertIsNone(self.jobs.enqueue_task(task_id, "123"))
        self.assertEqual(self.producer.tasks_due({"123"}), [])

    def test_cancelled_legacy_task_is_not_resurrected_by_its_job(self):
        task_id, job_id = self.task_job()
        self.tasks.fail(task_id, "owner cancelled")
        self.assertIsNone(self.jobs.claim(job_id))
        self.assertEqual(self.jobs.get(job_id)["status"], "cancelled")

    def test_initiative_generation_and_consumption_survive_restart(self):
        self.jobs.note_initiative(self.key, 5, "first context")
        self.jobs.note_initiative(self.key, 6, "new context")
        self.jobs.note_initiative(self.key, 4, "stale context")
        jobs = self.jobs.enqueue_initiatives({"123"})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(json.loads(self.jobs.get(jobs[0])["payload"])["context"], "new context")
        second = JobStore(self.path)
        try:
            self.assertEqual(second.enqueue_initiatives({"123"}), [])
            second.note_initiative(self.key, 7, "later")
            self.assertEqual(len(second.enqueue_initiatives({"123"})), 1)
        finally:
            second.close()

    def test_internal_events_never_collide_with_telegram_update_ids(self):
        self.task_job()
        first, _ = self.jobs.accept(self.key, 1, {"update": "telegram"})
        self.assertEqual(self.jobs.get(first)["kind"], "telegram_update")
        self.assertEqual(self.jobs.cursor("telegram", "123"), 2)
        with self.assertRaises(ValueError):
            self.jobs.accept(self.key, -1, {})

    def test_legacy_recovery_does_not_fail_linked_queued_or_deferred_work(self):
        task_id, job_id = self.task_job()
        self.jobs.claim(job_id)
        unlinked = self.tasks.add_task("456", 42, "old work")
        self.tasks.claim_next("456")
        self.assertEqual(self.jobs.recover_legacy_tasks(), 1)
        self.assertEqual(self.tasks.get(task_id)["status"], "running")
        self.assertEqual(self.tasks.get(unlinked)["status"], "failed")
        self.jobs.recover()
        self.assertEqual(self.jobs.get(job_id)["status"], "interrupted")
        self.assertEqual(self.tasks.get(task_id)["status"], "failed")


class TestBackgroundExecution(BackgroundFixture):
    def runtime(self, model):
        dispatcher = ConversationDispatcher()
        self.addCleanup(dispatcher.shutdown, 2)
        def session(job):
            runtime.checkpoint("session-job-" + str(job["id"]))
            return "session-job-" + str(job["id"])
        executor = BackgroundExecutor(self.jobs, session, model,
            lambda text: runtime.emit({"operation": "send", "text": text}))
        runtime = DurableRuntime(self.jobs, dispatcher, executor.execute, Mock(), Mock())
        return runtime

    def test_done_report_completes_job_and_legacy_task_together(self):
        task_id, job_id = self.task_job()
        runtime = self.runtime(Mock(return_value="**DONE** verified output"))
        runtime.run(job_id)
        self.assertEqual(self.jobs.get(job_id)["status"], "succeeded")
        self.assertEqual(self.tasks.get(task_id)["status"], "done")
        self.assertEqual(self.tasks.get(task_id)["steps"], 1)
        self.assertIn("verified output", self.jobs.get(job_id)["result"])

    def test_step_limit_without_done_is_failure_not_false_completion(self):
        task_id, job_id = self.task_job(maximum=1)
        runtime = self.runtime(Mock(return_value="still working"))
        runtime.run(job_id)
        self.assertEqual(self.jobs.get(job_id)["status"], "failed")
        self.assertEqual(self.tasks.get(task_id)["status"], "failed")

    def test_quota_deferral_resumes_only_completed_step_checkpoints(self):
        task_id, job_id = self.task_job()
        model = Mock(side_effect=["step one complete", RetryLater(15), "DONE verified"])
        runtime = self.runtime(model)
        runtime.run(job_id)
        self.assertEqual(self.jobs.get(job_id)["status"], "queued")
        self.assertEqual(self.tasks.get(task_id)["steps"], 1)
        self.assertEqual(self.jobs.pending(), [])
        self.now += 15
        runtime.run(job_id)
        self.assertEqual(self.tasks.get(task_id)["status"], "done")
        self.assertEqual(self.tasks.get(task_id)["steps"], 2)
        self.assertIn("Step 1/3", model.call_args_list[0].args[1])
        self.assertIn("Step 2/3", model.call_args_list[2].args[1])
        runs = list(self.jobs.db.execute("SELECT status FROM runtime_runs ORDER BY id"))
        self.assertEqual([row[0] for row in runs], ["deferred", "succeeded"])

    def test_ambiguous_step_is_never_replayed(self):
        task_id, job_id = self.task_job()
        model = Mock(side_effect=["step one complete", ExecutionUncertain()])
        runtime = self.runtime(model)
        runtime.run(job_id)
        runtime.run(job_id)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(self.jobs.get(job_id)["status"], "interrupted")
        self.assertEqual(self.tasks.get(task_id)["steps"], 1)

    def test_initiative_silent_is_persisted_success_without_delivery(self):
        self.jobs.note_initiative(self.key, 1, "quiet group")
        job_id = self.jobs.enqueue_initiatives({"123"})[0]
        self.runtime(Mock(return_value="SILENT")).run(job_id)
        self.assertEqual(self.jobs.get(job_id)["status"], "succeeded")
        self.assertEqual(self.jobs.get(job_id)["result"], "[]")
        self.assertIsNone(self.jobs.claim_delivery())


class TestRuntimeMigrationTwo(unittest.TestCase):
    def test_upgrade_preserves_version_one_jobs_and_cursors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "db")
            with sqlite3.connect(path) as db:
                db.executescript(MIGRATION_1)
                db.execute("CREATE TABLE runtime_migrations(version INTEGER PRIMARY KEY,applied_at REAL)")
                db.execute("INSERT INTO runtime_migrations VALUES(1,0)")
                db.execute("INSERT INTO runtime_cursors VALUES('telegram','123',9)")
                db.execute("INSERT INTO runtime_inbox VALUES(1,'telegram','123',8,'{}',0)")
                db.execute("INSERT INTO runtime_jobs(id,inbox_id,channel,account_id,conversation_id,status,created_at,updated_at) "
                           "VALUES(1,1,'telegram','123','42','queued',0,0)")
            jobs = JobStore(path)
            try:
                self.assertEqual(jobs.cursor("telegram", "123"), 9)
                self.assertEqual(jobs.get(1)["kind"], "telegram_update")
                self.assertEqual(jobs.get(1)["status"], "queued")
                self.assertEqual(jobs.get(1)["next_try"], 0)
            finally:
                jobs.close()
