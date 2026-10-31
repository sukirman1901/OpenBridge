"""Additive, versioned inbox/job/run/outbox storage.

All acceptance changes share one SQLite transaction. No legacy table or
credential is copied. Only the owning, single-poller bridge may run recovery.
"""
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

from openbridge.domain.messages import ConversationKey
from openbridge.domain.jobs import LANES, execution_lane


MIGRATION_1 = """
CREATE TABLE runtime_inbox (
    id INTEGER PRIMARY KEY, channel TEXT NOT NULL, account_id TEXT NOT NULL,
    source_id INTEGER NOT NULL, payload TEXT NOT NULL, received_at REAL NOT NULL,
    UNIQUE(channel, account_id, source_id)
);
CREATE TABLE runtime_cursors (
    channel TEXT NOT NULL, account_id TEXT NOT NULL, next_id INTEGER NOT NULL,
    PRIMARY KEY(channel, account_id)
);
CREATE TABLE runtime_jobs (
    id INTEGER PRIMARY KEY, inbox_id INTEGER NOT NULL UNIQUE REFERENCES runtime_inbox(id),
    channel TEXT NOT NULL, account_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
      ('queued','running','succeeded','failed','interrupted','cancelled')),
    ack_ready INTEGER NOT NULL DEFAULT 0, status_message_id INTEGER,
    session_id TEXT, result TEXT, error TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX runtime_jobs_queue ON runtime_jobs(status, id);
CREATE INDEX runtime_jobs_conversation ON runtime_jobs(channel, account_id, conversation_id, status);
CREATE TABLE runtime_runs (
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES runtime_jobs(id),
    status TEXT NOT NULL, session_id TEXT, error TEXT,
    started_at REAL NOT NULL, finished_at REAL
);
CREATE TABLE runtime_deliveries (
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES runtime_jobs(id),
    sequence INTEGER NOT NULL, channel TEXT NOT NULL, account_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','dead','uncertain')),
    attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL, error TEXT,
    created_at REAL NOT NULL, UNIQUE(job_id, sequence)
);
"""

MIGRATION_2 = """
ALTER TABLE runtime_jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'telegram_update';
ALTER TABLE runtime_jobs ADD COLUMN next_try REAL NOT NULL DEFAULT 0;
CREATE TABLE runtime_triggers (
    kind TEXT NOT NULL, channel TEXT NOT NULL, account_id TEXT NOT NULL,
    source_key TEXT NOT NULL, job_id INTEGER NOT NULL UNIQUE REFERENCES runtime_jobs(id),
    PRIMARY KEY(kind,channel,account_id,source_key)
);
CREATE TABLE runtime_steps (
    job_id INTEGER NOT NULL REFERENCES runtime_jobs(id), step INTEGER NOT NULL,
    answer TEXT NOT NULL, completed_at REAL NOT NULL,
    PRIMARY KEY(job_id,step)
);
CREATE TABLE runtime_initiative (
    channel TEXT NOT NULL, account_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
    generation INTEGER NOT NULL, enqueued_generation INTEGER NOT NULL DEFAULT 0,
    context TEXT NOT NULL, PRIMARY KEY(channel,account_id,conversation_id)
);
"""

MIGRATION_3 = """
ALTER TABLE runtime_jobs ADD COLUMN lane TEXT NOT NULL DEFAULT 'interactive'
    CHECK(lane IN ('interactive','background'));
UPDATE runtime_jobs SET lane='background' WHERE kind IN ('schedule','task','initiative');
CREATE INDEX runtime_jobs_lane_queue ON runtime_jobs(channel,account_id,conversation_id,lane,status,id);
CREATE INDEX runtime_jobs_lane_pending ON runtime_jobs(status,lane,id);
"""


class JobStore:
    def __init__(self, path=":memory:", clock=time.time):
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        with self.transaction():
            self.db.execute("CREATE TABLE IF NOT EXISTS runtime_migrations "
                            "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
            versions = [r[0] for r in self.db.execute("SELECT version FROM runtime_migrations")]
            if any(version > 3 for version in versions):
                raise RuntimeError("Runtime database requires a newer OpenBridge")
            if 1 not in versions:
                for statement in MIGRATION_1.split(";"):
                    if statement.strip():
                        self.db.execute(statement)
                self.db.execute("INSERT INTO runtime_migrations VALUES(1, ?)", (clock(),))
            if 2 not in versions:
                for statement in MIGRATION_2.split(";"):
                    if statement.strip():
                        self.db.execute(statement)
                self.db.execute("INSERT INTO runtime_migrations VALUES(2, ?)", (clock(),))
            if 3 not in versions:
                for statement in MIGRATION_3.split(";"):
                    if statement.strip():
                        self.db.execute(statement)
                self.db.execute("INSERT INTO runtime_migrations VALUES(3, ?)", (clock(),))

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def task_time(self):
        return datetime.fromtimestamp(self.clock(), timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")

    def cursor(self, channel, account_id, default=0):
        with self.lock:
            row = self.db.execute("SELECT next_id FROM runtime_cursors WHERE channel=? AND account_id=?",
                                  (channel, account_id)).fetchone()
            return row[0] if row else default

    def accept(self, key, source_id, payload, enqueue=True):
        """Return (job_id, newly_accepted). Cursor never advances without inbox."""
        if source_id < 0:
            raise ValueError("Negative source IDs are reserved for internal triggers")
        encoded = json.dumps(payload, ensure_ascii=False)
        now = self.clock()
        with self.transaction():
            row = self.db.execute(
                "SELECT i.id, j.id AS job_id FROM runtime_inbox i LEFT JOIN runtime_jobs j "
                "ON j.inbox_id=i.id WHERE i.channel=? AND i.account_id=? AND i.source_id=?",
                (key.channel, key.account_id, source_id)).fetchone()
            created = row is None
            if created:
                inbox = self.db.execute(
                    "INSERT INTO runtime_inbox(channel,account_id,source_id,payload,received_at) "
                    "VALUES(?,?,?,?,?)", (key.channel, key.account_id, source_id, encoded, now)).lastrowid
                job_id = None
                if enqueue:
                    job_id = self.db.execute(
                        "INSERT INTO runtime_jobs(inbox_id,channel,account_id,conversation_id,status,created_at,updated_at) "
                        "VALUES(?,?,?,?,'queued',?,?)",
                        (inbox, key.channel, key.account_id, key.conversation_id, now, now)).lastrowid
            else:
                job_id = row["job_id"]
            self.db.execute(
                "INSERT INTO runtime_cursors VALUES(?,?,?) ON CONFLICT(channel,account_id) "
                "DO UPDATE SET next_id=MAX(next_id,excluded.next_id)",
                (key.channel, key.account_id, source_id + 1))
        return job_id, created

    def _enqueue_trigger(self, key, kind, source_key, payload):
        """Caller owns the transaction. Internal events never advance channel cursors."""
        lane = execution_lane(kind)
        existing = self.db.execute("SELECT job_id FROM runtime_triggers WHERE kind=? AND channel=? "
                                   "AND account_id=? AND source_key=?",
                                   (kind, key.channel, key.account_id, source_key)).fetchone()
        if existing:
            return existing[0], False
        source_id = self.db.execute("SELECT MIN(COALESCE(MIN(source_id),0),0)-1 FROM runtime_inbox").fetchone()[0]
        now = self.clock()
        inbox = self.db.execute("INSERT INTO runtime_inbox(channel,account_id,source_id,payload,received_at) "
                                "VALUES(?,?,?,?,?)", (key.channel, key.account_id, source_id,
                                json.dumps(payload, ensure_ascii=False), now)).lastrowid
        job_id = self.db.execute("INSERT INTO runtime_jobs(inbox_id,channel,account_id,conversation_id,"
                                 "status,ack_ready,created_at,updated_at,kind,lane) VALUES(?,?,?,?,'queued',1,?,?,?,?)",
                                 (inbox, key.channel, key.account_id, key.conversation_id, now, now, kind, lane)).lastrowid
        self.db.execute("INSERT INTO runtime_triggers VALUES(?,?,?,?,?)",
                        (kind, key.channel, key.account_id, source_key, job_id))
        return job_id, True

    def enqueue_schedule(self, expected, occurrence, fired_at):
        with self.transaction():
            row = self.db.execute("SELECT * FROM schedules WHERE id=?", (expected["id"],)).fetchone()
            fields = ("bot_id", "chat_id", "expr", "kind", "prompt", "tz", "last_fired")
            if not row or not row["active"] or any(row[field] != expected[field] for field in fields):
                return None
            key = ConversationKey("telegram", str(row["bot_id"]), str(row["chat_id"]))
            job_id, created = self._enqueue_trigger(key, "schedule", occurrence, {
                "schedule_id": row["id"], "prompt": row["prompt"], "occurrence": occurrence})
            if created:
                self.db.execute("UPDATE schedules SET last_fired=? WHERE id=?", (fired_at, row["id"]))
            return job_id

    def enqueue_task(self, task_id, account_id):
        with self.transaction():
            existing = self.db.execute("SELECT job_id FROM runtime_triggers WHERE kind='task' "
                                       "AND channel='telegram' AND account_id=? AND source_key=?",
                                       (account_id, str(task_id))).fetchone()
            if existing:
                return existing[0]
            task = self.db.execute("SELECT * FROM tasks WHERE id=? AND bot_id=? AND status='queued'",
                                   (task_id, account_id)).fetchone()
            if not task:
                return None
            key = ConversationKey("telegram", account_id, str(task["chat_id"]))
            return self._enqueue_trigger(key, "task", str(task_id), {
                "task_id": task_id, "goal": task["goal"], "max_steps": task["max_steps"]})[0]

    def task_candidates(self, account_id):
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT t.id FROM tasks t WHERE t.bot_id=? AND t.status='queued' "
                "AND NOT EXISTS(SELECT 1 FROM runtime_triggers r WHERE r.kind='task' "
                "AND r.account_id=t.bot_id AND r.source_key=CAST(t.id AS TEXT)) ORDER BY t.id LIMIT 100", (account_id,))]

    def note_initiative(self, key, generation, context):
        with self.transaction():
            self.db.execute("INSERT INTO runtime_initiative(channel,account_id,conversation_id,generation,context) "
                "VALUES(?,?,?,?,?) ON CONFLICT(channel,account_id,conversation_id) DO UPDATE SET "
                "generation=excluded.generation,context=excluded.context WHERE excluded.generation>generation",
                (key.channel, key.account_id, key.conversation_id, generation, context))

    def enqueue_initiatives(self, account_ids):
        jobs = []
        with self.transaction():
            rows = self.db.execute("SELECT * FROM runtime_initiative WHERE generation>enqueued_generation").fetchall()
            for row in rows:
                if row["account_id"] not in account_ids:
                    continue
                key = ConversationKey(row["channel"], row["account_id"], row["conversation_id"])
                job_id, _ = self._enqueue_trigger(key, "initiative", f"{key.conversation_id}:{row['generation']}",
                                                 {"context": row["context"]})
                self.db.execute("UPDATE runtime_initiative SET enqueued_generation=generation "
                                "WHERE channel=? AND account_id=? AND conversation_id=?",
                                (key.channel, key.account_id, key.conversation_id))
                jobs.append(job_id)
        return jobs

    def completed_steps(self, job_id):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT step,answer FROM runtime_steps WHERE job_id=? ORDER BY step", (job_id,))]

    def record_step(self, job_id, step, answer):
        with self.transaction():
            job = self.get(job_id)
            if not job or job["status"] != "running" or job["kind"] != "task":
                raise RuntimeError("Step checkpoint requires a running task job")
            self.db.execute("INSERT INTO runtime_steps VALUES(?,?,?,?)", (job_id, step, answer, self.clock()))
            task_id = json.loads(job["payload"])["task_id"]
            self.db.execute("UPDATE tasks SET steps=?,updated_at=? WHERE id=?", (step + 1, self.task_time(), task_id))

    def defer(self, job_id, delay):
        with self.transaction():
            self.db.execute("UPDATE runtime_jobs SET status='queued',next_try=?,updated_at=? WHERE id=? AND status='running'",
                            (self.clock() + delay, self.clock(), job_id))
            self.db.execute("UPDATE runtime_runs SET status='deferred',finished_at=? WHERE job_id=? AND status='running'",
                            (self.clock(), job_id))
            job = self.get(job_id)
            if job and job["kind"] == "task":
                self.db.execute("UPDATE tasks SET status='queued',updated_at=? WHERE id=?",
                                (self.task_time(), json.loads(job["payload"])["task_id"]))

    def acknowledge(self, job_id, message_id=None):
        with self.transaction():
            self.db.execute("UPDATE runtime_jobs SET ack_ready=1,status_message_id=? WHERE id=? AND status='queued'",
                            (message_id, job_id))

    def get(self, job_id):
        with self.lock:
            row = self.db.execute(
                "SELECT j.*,i.payload FROM runtime_jobs j JOIN runtime_inbox i ON i.id=j.inbox_id WHERE j.id=?",
                (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, account_id, conversation_id, limit=10, channel="telegram"):
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT id,kind,lane,status,session_id,error,(SELECT GROUP_CONCAT(DISTINCT status) "
                "FROM runtime_deliveries WHERE job_id=runtime_jobs.id) AS delivery_state "
                "FROM runtime_jobs WHERE channel=? "
                "AND account_id=? AND conversation_id=? ORDER BY id DESC LIMIT ?",
                (channel, account_id, str(conversation_id), limit))]

    def pending(self, limit=128, lane=None):
        if lane is not None and lane not in LANES:
            raise ValueError("Unknown execution lane")
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT j.* FROM runtime_jobs j WHERE j.status='queued' AND j.ack_ready=1 AND j.next_try<=? "
                "AND (? IS NULL OR j.lane=?) "
                # Reminders (schedule kind) stay fenced behind interrupted work but
                # may pass the same-lane conversation leader; earliest schedule first.
                "AND NOT EXISTS(SELECT 1 FROM runtime_jobs b WHERE b.channel=j.channel "
                "AND b.account_id=j.account_id AND b.conversation_id=j.conversation_id "
                "AND b.status='interrupted') "
                "AND (j.kind='schedule' OR NOT EXISTS(SELECT 1 FROM runtime_jobs b WHERE b.channel=j.channel "
                "AND b.account_id=j.account_id AND b.conversation_id=j.conversation_id AND b.lane=j.lane "
                "AND (b.status='running' OR (b.status='queued' AND b.id<j.id)))) "
                "ORDER BY (j.kind!='schedule'), j.id LIMIT ?",
                (self.clock(), lane, lane, limit))]

    def is_blocked(self, key):
        with self.lock:
            return self.db.execute("SELECT 1 FROM runtime_jobs WHERE channel=? AND account_id=? "
                                   "AND conversation_id=? AND status='interrupted' LIMIT 1",
                                   (key.channel, key.account_id, key.conversation_id)).fetchone() is not None

    def claim(self, job_id):
        with self.transaction():
            job = self.get(job_id)
            if not job or job["status"] != "queued" or not job["ack_ready"] or job["next_try"] > self.clock():
                return None
            # Fencing always applies: interrupted work blocks the conversation.
            if self.db.execute(
                    "SELECT 1 FROM runtime_jobs WHERE channel=? AND account_id=? AND conversation_id=? "
                    "AND status='interrupted' LIMIT 1",
                    (job["channel"], job["account_id"], job["conversation_id"])).fetchone():
                return None
            # Reminders may interleave with the conversation leader; nothing else may.
            if job["kind"] != "schedule" and self.db.execute(
                    "SELECT 1 FROM runtime_jobs WHERE channel=? AND account_id=? AND conversation_id=? "
                    "AND lane=? AND (status='running' OR (status='queued' AND id<?)) LIMIT 1",
                    (job["channel"], job["account_id"], job["conversation_id"],
                     job["lane"], job_id)).fetchone():
                return None
            if job["kind"] == "task":
                task = self.db.execute("SELECT status FROM tasks WHERE id=? AND bot_id=? AND CAST(chat_id AS TEXT)=?",
                    (json.loads(job["payload"])["task_id"], job["account_id"], job["conversation_id"])).fetchone()
                if not task or task[0] not in ("queued", "running"):
                    self.db.execute("UPDATE runtime_jobs SET status='cancelled' WHERE id=?", (job_id,))
                    return None
            now = self.clock()
            self.db.execute("UPDATE runtime_jobs SET status='running',updated_at=? WHERE id=?", (now, job_id))
            run_id = self.db.execute(
                "INSERT INTO runtime_runs(job_id,status,started_at) VALUES(?,'running',?)", (job_id, now)).lastrowid
            job.update(status="running", run_id=run_id)
            if job["kind"] == "task":
                self.db.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?",
                                (self.task_time(), json.loads(job["payload"])["task_id"]))
            return job

    def checkpoint(self, job_id, session_id):
        with self.transaction():
            self.db.execute("UPDATE runtime_jobs SET session_id=? WHERE id=? AND status='running'", (session_id, job_id))
            self.db.execute("UPDATE runtime_runs SET session_id=? WHERE job_id=? AND status='running'", (session_id, job_id))

    def cancel_queued(self, job_id):
        """Cancel durable work that has not started running yet.

        Only queued jobs (including quota-deferred ones) are cancellable; a
        running claim always wins and returns False. The claim fence makes the
        in-memory dispatcher safe: a submitted future whose job was cancelled
        simply claims nothing and exits."""
        now = self.clock()
        with self.transaction():
            cur = self.db.execute(
                "UPDATE runtime_jobs SET status='cancelled',updated_at=? WHERE id=? AND status='queued'",
                (now, job_id))
            if not cur.rowcount:
                return False
            self.db.execute(
                "UPDATE runtime_runs SET status='cancelled',finished_at=? "
                "WHERE job_id=? AND status='deferred'", (now, job_id))
            return True

    def finish(self, job_id, outputs, status="succeeded", error=None):
        if status not in ("succeeded", "failed", "interrupted"):
            raise ValueError("Invalid terminal job status")
        now = self.clock()
        with self.transaction():
            job = self.get(job_id)
            if not job or job["status"] != "running":
                return False
            self.db.execute("UPDATE runtime_jobs SET status=?,result=?,error=?,updated_at=? WHERE id=?",
                            (status, json.dumps(outputs, ensure_ascii=False), error, now, job_id))
            self.db.execute("UPDATE runtime_runs SET status=?,error=?,finished_at=? WHERE job_id=? AND status='running'",
                            (status, error, now, job_id))
            self._project_task(job, status, outputs, error)
            for index, output in enumerate(outputs):
                output = dict(output)
                if index == 0 and output["operation"] == "send" and job["status_message_id"]:
                    output.update(operation="edit", message_id=job["status_message_id"])
                self.db.execute(
                    "INSERT INTO runtime_deliveries(job_id,sequence,channel,account_id,conversation_id,payload,"
                    "status,next_try,created_at) VALUES(?,?,?,?,?,?,'pending',?,?)",
                    (job_id, index, job["channel"], job["account_id"], job["conversation_id"],
                     json.dumps(output, ensure_ascii=False), now, now))
        return True

    def _project_task(self, job, status, outputs, error):
        if job["kind"] != "task":
            return
        task_id = json.loads(job["payload"])["task_id"]
        result = "\n\n".join(output.get("text", "") for output in outputs)[:6000]
        self.db.execute("UPDATE tasks SET status=?,result=?,last_error=?,updated_at=? WHERE id=?",
                        ("done" if status == "succeeded" else "failed", result, error, self.task_time(), task_id))

    def recover(self):
        """Run once under the process lock, before workers. Never replay running work."""
        with self.transaction():
            now = self.clock()
            interrupted = [dict(r) for r in self.db.execute("SELECT * FROM runtime_jobs WHERE status='running'")]
            self.db.execute("UPDATE runtime_jobs SET status='interrupted',error='process interrupted',updated_at=? "
                            "WHERE status='running'", (now,))
            self.db.execute("UPDATE runtime_runs SET status='interrupted',error='process interrupted',finished_at=? "
                            "WHERE status='running'", (now,))
            self.db.execute("UPDATE runtime_jobs SET ack_ready=1 WHERE status='queued'")
            for job in interrupted:
                self._project_task(self.get(job["id"]), "interrupted", [], "Execution interrupted; review the durable job")
                output = {"operation": "send", "text": f"⚠️ Job #{job['id']} was interrupted. "
                          "It will not be replayed automatically. Review it with /jobs and /resolve."}
                if job["status_message_id"]:
                    output.update(operation="edit", message_id=job["status_message_id"])
                self.db.execute("INSERT OR IGNORE INTO runtime_deliveries(job_id,sequence,channel,account_id,"
                                "conversation_id,payload,status,next_try,created_at) VALUES(?,0,?,?,?,?, 'pending',?,?)",
                                (job["id"], job["channel"], job["account_id"], job["conversation_id"],
                                 json.dumps(output), now, now))
            for row in self.db.execute("SELECT id,payload FROM runtime_deliveries WHERE status='sending'").fetchall():
                operation = json.loads(row["payload"])["operation"]
                self.db.execute("UPDATE runtime_deliveries SET status=?,error='delivery interrupted' WHERE id=?",
                                ("pending" if operation == "edit" else "uncertain", row["id"]))
        return interrupted

    def resolve(self, job_id):
        """Explicit operator reconciliation, not an execution retry."""
        with self.transaction():
            return self.db.execute("UPDATE runtime_jobs SET status='cancelled',updated_at=? "
                                   "WHERE id=? AND status='interrupted'", (self.clock(), job_id)).rowcount == 1

    def recover_legacy_tasks(self):
        """Pre-runtime running tasks lack checkpoints and must never be replayed."""
        with self.transaction():
            return self.db.execute("UPDATE tasks SET status='failed',last_error='Legacy execution interrupted before durable migration' "
                "WHERE status='running' AND NOT EXISTS(SELECT 1 FROM runtime_triggers r "
                "WHERE r.kind='task' AND r.account_id=tasks.bot_id AND r.source_key=CAST(tasks.id AS TEXT))").rowcount

    def prune_terminal(self, older_than=30 * 86400, limit=500):
        """Settled-job retention, per job in one transaction.

        Only terminal jobs older than `older_than` seconds whose every delivery
        reached a final state (sent/dead) may be deleted, together with their
        dependent steps, runs, deliveries, trigger, and inbox rows. Interrupted,
        queued, and running jobs — and pending/sending/uncertain deliveries —
        are never pruned, because their work or outcome may still need review.
        """
        cutoff = self.clock() - older_than
        pruned = []
        with self.transaction():
            rows = self.db.execute(
                "SELECT j.id, j.inbox_id FROM runtime_jobs j WHERE j.status IN "
                "('succeeded','failed','cancelled') AND j.updated_at<=? AND NOT EXISTS "
                "(SELECT 1 FROM runtime_deliveries d WHERE d.job_id=j.id AND "
                "d.status IN ('pending','sending','uncertain')) ORDER BY j.updated_at,j.id LIMIT ?",
                (cutoff, limit)).fetchall()
            for job_id, inbox_id in rows:
                for statement, arguments in (
                    ("DELETE FROM runtime_steps WHERE job_id=?", (job_id,)),
                    ("DELETE FROM runtime_runs WHERE job_id=?", (job_id,)),
                    ("DELETE FROM runtime_deliveries WHERE job_id=?", (job_id,)),
                    ("DELETE FROM runtime_triggers WHERE job_id=?", (job_id,)),
                    ("DELETE FROM runtime_jobs WHERE id=?", (job_id,)),
                    ("DELETE FROM runtime_inbox WHERE id=?", (inbox_id,)),
                ):
                    self.db.execute(statement, arguments)
                pruned.append(job_id)
        return pruned

    def claim_delivery(self):
        with self.transaction():
            row = self.db.execute(
                "SELECT d.* FROM runtime_deliveries d JOIN runtime_jobs j ON j.id=d.job_id "
                "WHERE d.status='pending' AND d.next_try<=? "
                "AND NOT EXISTS(SELECT 1 FROM runtime_deliveries p WHERE p.job_id=d.job_id "
                "AND p.sequence<d.sequence AND p.status!='sent') "
                "ORDER BY CASE j.lane WHEN 'interactive' THEN 0 ELSE 1 END, "
                "CASE j.kind WHEN 'schedule' THEN 0 ELSE 1 END, d.id LIMIT 1", (self.clock(),)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE runtime_deliveries SET status='sending',attempts=attempts+1 WHERE id=?", (row["id"],))
            delivery = dict(row)
            delivery["attempts"] += 1
            delivery["payload"] = json.loads(delivery["payload"])
            return delivery

    def delivery_result(self, delivery, outcome, error=None, retry_after=None):
        if outcome not in ("sent", "pending", "dead", "uncertain"):
            raise ValueError("Invalid delivery outcome")
        if outcome == "pending" and delivery["attempts"] >= 6:
            outcome = "dead"
        delay = max(min(300, 2 ** delivery["attempts"]), retry_after or 0)
        with self.transaction():
            self.db.execute("UPDATE runtime_deliveries SET status=?,error=?,next_try=? WHERE id=? AND status='sending'",
                            (outcome, error, self.clock() + delay, delivery["id"]))
