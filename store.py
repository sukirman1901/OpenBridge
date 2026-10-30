#!/usr/bin/env python3
"""
Store — SQLite storage layer for the Telegram bridge bot.
Replaces the JSON files (cron.json, chats.json, etc.) with a single database:
- schedules : cron/once/interval schedules per bot
- outbox    : message send queue (retry + backoff + dead-letter)
- seen      : last getUpdates offset per bot (survives restarts)
- usage     : per-bot per-day usage (calls, cost, tokens)

Atomic per operation: a single connection with check_same_thread=False +
threading.Lock, where each call is one transaction (execute + commit
under the lock).
"""
import os
import sqlite3
import threading
import time
from datetime import datetime

KINDS = ("cron", "once", "interval")
OUTBOX_STATUS = ("pending", "sent", "dead")
MAX_BACKOFF = 300  # seconds; attempt n → next_try = now + min(300, 2**n)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id          INTEGER PRIMARY KEY,
    bot_id      TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    expr        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK(kind IN ('cron','once','interval')),
    tz          TEXT NOT NULL DEFAULT 'Asia/Jakarta',
    prompt      TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    last_fired  TEXT,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY,
    token       TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_try    REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','sent','dead')),
    created_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_outbox_pending
    ON outbox(token, chat_id, text) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS seen (
    bot_id         TEXT PRIMARY KEY,
    last_update_id INTEGER
);
CREATE TABLE IF NOT EXISTS usage (
    bot_id TEXT,
    day    TEXT,
    calls  INTEGER DEFAULT 0,
    cost   REAL DEFAULT 0,
    tokens INTEGER DEFAULT 0,
    PRIMARY KEY(bot_id, day)
);
CREATE TABLE IF NOT EXISTS chats (
    chat_id    TEXT PRIMARY KEY,
    judul      TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS personas (
    nama       TEXT PRIMARY KEY,
    persona    TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT
);
"""

_NOW = "%Y-%m-%d %H:%M:%S"


def iso_now(ts=None):
    """Epoch seconds → 'YYYY-MM-DD HH:MM:SS' (local timezone, same style as the bridge)."""
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime(_NOW)


def interval_seconds(expr):
    """Simple parser for interval exprs: '90' (seconds) or '5m','2h','3d'.
    None when it cannot be parsed — let the server evaluate it."""
    s = str(expr or "").strip().lower()
    if not s:
        return None
    if s.endswith(("s", "m", "h", "d")):
        try:
            n = float(s[:-1])
            return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[s[-1]]
        except (ValueError, KeyError):
            return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


class Store:
    """SQLite storage layer for the bridge. path=None → :memory: database."""

    def __init__(self, path=None):
        self._path = path or ":memory:"
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

    # ── schedules ────────────────────────────────────────────────
    def add_schedule(self, bot_id, chat_id, expr, kind, prompt, tz="Asia/Jakarta",
                     active=1, now=None):
        """Add a schedule and return its new id. kind: 'cron' | 'once' | 'interval'."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got: {kind!r}")
        cur = self._exec(
            "INSERT INTO schedules(bot_id, chat_id, expr, kind, tz, prompt,"
            " active, last_fired, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (str(bot_id), int(chat_id), str(expr), kind, str(tz or "Asia/Jakarta"),
             str(prompt), 1 if active else 0, None, iso_now(now)))
        return cur.lastrowid

    def list_schedules(self, bot_id=None, active_only=False):
        """All schedules (optionally per bot). For the /schedule panel."""
        q, p = [], []
        sql = "SELECT * FROM schedules"
        if bot_id is not None:
            q.append("bot_id = ?"); p.append(str(bot_id))
        if active_only:
            q.append("active = 1")
        if q:
            sql += " WHERE " + " AND ".join(q)
        sql += " ORDER BY id"
        return self._query(sql, p)

    def due_schedules(self, bot_id=None, kind=None, now=None):
        """Active schedules that are due, according to kind.
        - 'cron'    : passed through as-is (the server evaluates the HH:MM pattern).
        - 'once'    : expr <= now and never fired yet (last_fired NULL).
        - 'interval': simple seconds/'m'/'h'/'d' parsing vs last_fired;
                      an expr that cannot be parsed is forwarded to the server.
        """
        now = now or datetime.now().strftime(_NOW)
        q, p = ["active = 1"], []
        if bot_id is not None:
            q.append("bot_id = ?"); p.append(str(bot_id))
        if kind is not None:
            if kind not in KINDS:
                raise ValueError(f"unknown kind: {kind!r}")
            q.append("kind = ?"); p.append(kind)
        rows = self._query("SELECT * FROM schedules WHERE " + " AND ".join(q) + " ORDER BY id", p)
        if kind == "once":
            rows = [r for r in rows if (r["last_fired"] or "") == "" and str(r["expr"]) <= str(now)]
        elif kind == "interval":
            hit = []
            for r in rows:
                detik = interval_seconds(r["expr"])
                if detik is None:
                    hit.append(r)  # the server evaluates this one
                    continue
                if not r["last_fired"]:
                    hit.append(r)
                    continue
                try:
                    lalu = datetime.strptime(str(r["last_fired"]), _NOW).timestamp()
                except ValueError:
                    hit.append(r)
                    continue
                if time.time() - lalu >= detik:
                    hit.append(r)
            rows = hit
        return rows

    def mark_fired(self, schedule_id, when=None):
        """Record that a schedule has fired (last_fired). True when the id exists."""
        cur = self._exec("UPDATE schedules SET last_fired = ? WHERE id = ?",
                         (str(when) if when is not None else iso_now(), schedule_id))
        return cur.rowcount > 0

    def delete_schedule(self, schedule_id):
        """Delete a schedule. True when a row was actually removed."""
        return self._exec("DELETE FROM schedules WHERE id = ?", (schedule_id,)).rowcount > 0

    # ── outbox ───────────────────────────────────────────────────
    def enqueue(self, token, chat_id, text, now=None):
        """Put a message into the send queue. Idempotent: an identical pending
        entry (token, chat_id, text) is not duplicated — UPSERT, returns the
        existing id. A message ready to send goes out immediately (next_try = now)."""
        t = time.time() if now is None else now
        with self._lock:
            ada = self._db.execute(
                "SELECT id FROM outbox WHERE token = ? AND chat_id = ? AND text = ?"
                " AND status = 'pending'", (token, int(chat_id), text)).fetchone()
            if ada:
                self._db.commit()
                return ada["id"]
            cur = self._db.execute(
                "INSERT INTO outbox(token, chat_id, text, attempts, next_try, status,"
                " created_at) VALUES(?,?,?,0,?,'pending',?)",
                (token, int(chat_id), str(text), float(t), iso_now(t)))
            self._db.commit()
            return cur.lastrowid

    def due_outbox(self, now=None, limit=None):
        """Claim pending messages whose next_try has passed:
        attempts+1 and next_try pushed forward (backoff, attempt n → now + min(300, 2**n)).
        Returns the claimed messages (at-least-once); finalized via mark_sent/mark_dead.
        """
        t = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM outbox WHERE status = 'pending' AND next_try <= ?"
                " ORDER BY id" + (f" LIMIT {int(limit)}" if limit else ""),
                (float(t),)).fetchall()
            hit = []
            for r in rows:
                percobaan = r["attempts"] + 1
                self._db.execute(
                    "UPDATE outbox SET attempts = ?, next_try = ? WHERE id = ?",
                    (percobaan, float(t) + min(MAX_BACKOFF, 2 ** percobaan), r["id"]))
                hit.append(dict(r))
            self._db.commit()
            return hit

    def mark_sent(self, outbox_id):
        """pending → sent. Idempotent: a row that is not pending is left untouched."""
        return self._exec("UPDATE outbox SET status = 'sent' WHERE id = ?"
                          " AND status = 'pending'", (outbox_id,)).rowcount > 0

    def mark_dead(self, outbox_id):
        """pending → dead (dead-letter after retries are exhausted). Idempotent."""
        return self._exec("UPDATE outbox SET status = 'dead' WHERE id = ?"
                          " AND status = 'pending'", (outbox_id,)).rowcount > 0

    def outbox_count(self, status=None):
        q, p = [], []
        if status is not None:
            q.append("status = ?"); p.append(status)
        sql = "SELECT COUNT(*) AS n FROM outbox" + (" WHERE " + " AND ".join(q) if q else "")
        with self._lock:
            return self._db.execute(sql, p).fetchone()["n"]

    # ── seen (getUpdates offset) ─────────────────────────────────
    def set_offset(self, bot_id, update_id):
        """Store the last getUpdates offset per bot. Idempotent UPSERT."""
        self._exec(
            "INSERT INTO seen(bot_id, last_update_id) VALUES(?,?)"
            " ON CONFLICT(bot_id) DO UPDATE SET last_update_id = excluded.last_update_id",
            (str(bot_id), int(update_id)))

    def get_offset(self, bot_id):
        """The bot's last offset; 0 when never stored."""
        with self._lock:
            r = self._db.execute("SELECT last_update_id FROM seen WHERE bot_id = ?",
                                 (str(bot_id),)).fetchone()
        return int(r["last_update_id"]) if r else 0

    # ── usage ────────────────────────────────────────────────────
    def add_usage(self, bot_id, day, calls=0, cost=0.0, tokens=0):
        """Tally usage per bot per day. UPSERT: values accumulate (increment)."""
        self._exec(
            "INSERT INTO usage(bot_id, day, calls, cost, tokens) VALUES(?,?,?,?,?)"
            " ON CONFLICT(bot_id, day) DO UPDATE SET"
            " calls = calls + excluded.calls,"
            " cost = cost + excluded.cost,"
            " tokens = tokens + excluded.tokens",
            (str(bot_id), str(day), int(calls), float(cost), int(tokens)))

    def get_usage(self, bot_id, day=None):
        """Bot usage: a dict for a specific day (zero by default) or aggregated over all days."""
        with self._lock:
            if day is not None:
                r = self._db.execute(
                    "SELECT bot_id, day, calls, cost, tokens FROM usage"
                    " WHERE bot_id = ? AND day = ?", (str(bot_id), str(day))).fetchone()
                return (dict(r) if r else {"bot_id": str(bot_id), "day": str(day),
                                           "calls": 0, "cost": 0.0, "tokens": 0})
            r = self._db.execute(
                "SELECT bot_id, COALESCE(SUM(calls),0) AS calls,"
                " COALESCE(SUM(cost),0) AS cost, COALESCE(SUM(tokens),0) AS tokens"
                " FROM usage WHERE bot_id = ?", (str(bot_id),)).fetchone()
        return {"bot_id": str(bot_id), "day": None, "calls": r["calls"],
                "cost": r["cost"], "tokens": r["tokens"]}

    # ── chats (chats known to the bridge) ────────────────────────
    def save_chat(self, chat_id, judul, ts=None):
        """Record/upsert a chat title. chat_id is stored as a string (JSON-safe)."""
        self._exec(
            "INSERT INTO chats(chat_id, judul, updated_at) VALUES(?,?,?)"
            " ON CONFLICT(chat_id) DO UPDATE SET"
            " judul = excluded.judul, updated_at = excluded.updated_at",
            (str(chat_id), str(judul), iso_now(ts)))

    def list_chats(self):
        """All chats, newest first (rowid as a tiebreaker for equal seconds)."""
        return self._query(
            "SELECT chat_id, judul, updated_at FROM chats ORDER BY updated_at DESC, rowid DESC")

    # ── personas (per name) ─────────────────────────────────────
    def upsert_persona(self, nama, persona, ts=None):
        if not nama or not persona:
            raise ValueError("both nama and persona are required")
        now_ = iso_now(ts)
        self._exec(
            "INSERT INTO personas(nama, persona, created_at, updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(nama) DO UPDATE SET"
            " persona = excluded.persona, updated_at = excluded.updated_at",
            (str(nama), str(persona), now_, now_))

    def list_personas(self):
        """All registered personas sorted by name (dicts: nama, persona, created_at, updated_at)."""
        return self._query(
            "SELECT nama, persona, created_at, updated_at FROM personas ORDER BY nama")

    def delete_persona(self, nama):
        """Remove a persona. Archiving does NOT happen here — the caller (the bridge) writes the trail."""
        cur = self._exec("DELETE FROM personas WHERE nama = ?", (str(nama),))
        return cur.rowcount > 0
