#!/usr/bin/env python3
"""
TaskQueue — per-bot autonomous task queue (cycle: goal → plan → act → verify
→ report, with a limited number of steps). An extra `tasks` table in the
bridge database: CREATE TABLE IF NOT EXISTS, never touches the old tables
(schedules, outbox, etc.).

Task statuses (stored DB values, shared with the bridge): 'queued' → 'running'
→ 'done' | 'failed'. The bridge claims the oldest queued task (claim_next),
runs the cycle step by step (bump after each step; quota exhausted → final
status), then closes it via complete/fail. Atomic per operation: a
single connection with check_same_thread=False + threading.Lock, where each
call is one transaction (execute + commit under the lock).
"""
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

STATUS = ("queued", "running", "done", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY,
    bot_id     TEXT NOT NULL,
    chat_id    INTEGER NOT NULL,
    goal       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'queued'
               CHECK(status IN ('queued','running','done','failed')),
    steps      INTEGER NOT NULL DEFAULT 0,
    max_steps  INTEGER NOT NULL DEFAULT 5,
    result     TEXT,
    last_error TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_tasks_bot_status
    ON tasks(bot_id, status, id);
"""

_NOW = "%Y-%m-%d %H:%M:%S"

# Constant UTC+7 (WIB) — ignores the system zone, same style as the bridge.
_TZ7 = timezone(timedelta(hours=7))


def iso_from_ts(ts=None):
    """Epoch seconds → 'YYYY-MM-DD HH:MM:SS' in constant UTC+7."""
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), _TZ7)
    return dt.strftime(_NOW)


class TaskQueue:
    """Per-bot autonomous task queue. path=None → :memory: database."""

    def __init__(self, db_path=None):
        self._path = db_path or ":memory:"
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ── internal ─────────────────────────────────────────────────
    def _exec(self, sql, params=()):
        """Writes: a single atomic transaction under the lock. Returns the cursor."""
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _query(self, sql, params=()):
        """Reads: atomic SELECT under the lock. Returns a list of dicts."""
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def close(self):
        with self._lock:
            self._db.commit()
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ── task queue ───────────────────────────────────────────────
    def add_task(self, bot_id, chat_id, goal, max_steps=5, now=None):
        """Insert a new task (status 'queued', steps=0) and return its id."""
        t = iso_from_ts(now)
        cur = self._exec(
            "INSERT INTO tasks(bot_id, chat_id, goal, status, steps, max_steps,"
            " created_at, updated_at) VALUES(?,?,?,'queued',0,?,?,?)",
            (str(bot_id), int(chat_id), str(goal), int(max_steps), t, t))
        return cur.lastrowid

    def claim_next(self, bot_id):
        """Claim the bot's oldest 'queued' task → status 'running' (atomic, a
        single transaction: SELECT then UPDATE under the lock). None when empty."""
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM tasks WHERE bot_id = ? AND status = 'queued'"
                " ORDER BY id LIMIT 1", (str(bot_id),)).fetchone()
            if r is None:
                self._db.commit()
                return None
            self._db.execute(
                "UPDATE tasks SET status = 'running', updated_at = ? WHERE id = ?",
                (iso_from_ts(), r["id"]))
            self._db.commit()
            now_ = dict(r)
            now_["status"] = "running"
            return now_

    def bump(self, task_id):
        """Advance the step counter of a task currently 'running'.
        - 'ok'    : steps+1 is still < max_steps (steps remain available).
        - 'full' : the next step reaches the max → status immediately becomes
                    'failed' with last_error='exceeded max_steps' (the bridge
                    stops iterating); also stable for tasks that are already final.
        - 'hilang': unknown task_id.
        """
        with self._lock:
            r = self._db.execute("SELECT * FROM tasks WHERE id = ?",
                                 (task_id,)).fetchone()
            if r is None:
                self._db.commit()
                return "hilang"
            if r["status"] in ("done", "failed"):
                # Already final — do not touch it again; report 'full' so it stops.
                self._db.commit()
                return "full"
            langkah = int(r["steps"]) + 1
            updated = iso_from_ts()
            if langkah < int(r["max_steps"]):
                self._db.execute(
                    "UPDATE tasks SET steps = ?, updated_at = ? WHERE id = ?",
                    (langkah, updated, task_id))
                self._db.commit()
                return "ok"
            self._db.execute(
                "UPDATE tasks SET steps = ?, status = 'failed',"
                " last_error = 'exceeded max_steps', updated_at = ? WHERE id = ?",
                (langkah, updated, task_id))
            self._db.commit()
            return "full"

    def complete(self, task_id, result):
        """'running' (or still 'queued') → 'done', storing result. Idempotent:
        tasks already finished/failed are left untouched. True only when newly closed."""
        cur = self._exec(
            "UPDATE tasks SET status = 'done', result = ?, updated_at = ?"
            " WHERE id = ? AND status NOT IN ('done','failed')",
            ("" if result is None else str(result), iso_from_ts(), task_id))
        return cur.rowcount > 0

    def fail(self, task_id, err):
        """'running' (or still 'queued') → 'failed', storing last_error. Idempotent."""
        cur = self._exec(
            "UPDATE tasks SET status = 'failed', last_error = ?, updated_at = ?"
            " WHERE id = ? AND status NOT IN ('done','failed')",
            ("" if err is None else str(err), iso_from_ts(), task_id))
        return cur.rowcount > 0

    def get(self, task_id):
        """A single task (dict with every column) or None when absent."""
        with self._lock:
            r = self._db.execute("SELECT * FROM tasks WHERE id = ?",
                                 (task_id,)).fetchone()
        return dict(r) if r else None

    def list_tasks(self, bot_id=None, status=None, limit=50):
        """List tasks newest first (optionally filtered by bot_id and/or status)."""
        q, p = [], []
        if bot_id is not None:
            q.append("bot_id = ?"); p.append(str(bot_id))
        if status is not None:
            q.append("status = ?"); p.append(status)
        sql = "SELECT * FROM tasks" + (" WHERE " + " AND ".join(q) if q else "")
        sql += " ORDER BY id DESC LIMIT ?"
        p.append(int(limit))
        return self._query(sql, p)

    def count(self, status=None, bot_id=None):
        """Task count, optionally filtered by status and/or bot_id."""
        q, p = [], []
        if status is not None:
            q.append("status = ?"); p.append(status)
        if bot_id is not None:
            q.append("bot_id = ?"); p.append(str(bot_id))
        sql = "SELECT COUNT(*) AS n FROM tasks" + (" WHERE " + " AND ".join(q) if q else "")
        with self._lock:
            return self._db.execute(sql, p).fetchone()["n"]
