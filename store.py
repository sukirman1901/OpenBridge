#!/usr/bin/env python3
"""
Store — layer penyimpanan SQLite untuk bridge bot Telegram.
Pengganti file-file JSON (cron.json, chats.json, dll) dengan satu database:
- schedules : jadwal cron/once/interval per bot
- outbox    : antrian kirim pesan (retry + backoff + dead-letter)
- seen      : offset getUpdates terakhir per bot (tahan restart)
- usage     : pemakaian per bot per hari (calls, cost, tokens)

Atomic per operasi: satu koneksi check_same_thread=False + threading.Lock,
tiap call = satu transaksi (execute + commit di bawah lock).
"""
import os
import sqlite3
import threading
import time
from datetime import datetime

KINDS = ("cron", "once", "interval")
OUTBOX_STATUS = ("pending", "sent", "dead")
MAX_BACKOFF = 300  # detik; attempt n → next_try = now + min(300, 2**n)

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
    """Detik epoch → 'YYYY-MM-DD HH:MM:SS' (timezone lokal, sama gaya bridge)."""
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime(_NOW)


def interval_seconds(expr):
    """Parsing sederhana expr interval: '90' (detik) atau '5m','2h','3d'.
    None kalau tidak bisa diparse — biarkan server yang evaluasi."""
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
    """Layer penyimpanan SQLite untuk bridge. path=None → database :memory:."""

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
        """Tulis: satu transaksi atomic di bawah lock. Balikin cursor."""
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _query(self, sql, params=()):
        """Baca: SELECT atomic di bawah lock. Balikin list dict."""
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
        """Tambah jadwal, balikin id barunya. kind: 'cron' | 'once' | 'interval'."""
        if kind not in KINDS:
            raise ValueError(f"kind harus salah satu dari {KINDS}, dapat: {kind!r}")
        cur = self._exec(
            "INSERT INTO schedules(bot_id, chat_id, expr, kind, tz, prompt,"
            " active, last_fired, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (str(bot_id), int(chat_id), str(expr), kind, str(tz or "Asia/Jakarta"),
             str(prompt), 1 if active else 0, None, iso_now(now)))
        return cur.lastrowid

    def list_schedules(self, bot_id=None, active_only=False):
        """Semua jadwal (opsional per bot). Untuk panel /jadwal."""
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
        """Jadwal aktif yang jatuh tempo menurut kind.
        - 'cron'    : dilempar apa adanya (pola HH:MM dievaluasi server).
        - 'once'    : expr <= now dan belum pernah terbakar (last_fired NULL).
        - 'interval': parsing sederhana detik/'m'/'h'/'d' vs last_fired;
                      expr tak bisa diparse → diteruskan ke server.
        """
        now = now or datetime.now().strftime(_NOW)
        q, p = ["active = 1"], []
        if bot_id is not None:
            q.append("bot_id = ?"); p.append(str(bot_id))
        if kind is not None:
            if kind not in KINDS:
                raise ValueError(f"kind tak dikenal: {kind!r}")
            q.append("kind = ?"); p.append(kind)
        rows = self._query("SELECT * FROM schedules WHERE " + " AND ".join(q) + " ORDER BY id", p)
        if kind == "once":
            rows = [r for r in rows if (r["last_fired"] or "") == "" and str(r["expr"]) <= str(now)]
        elif kind == "interval":
            kena = []
            for r in rows:
                detik = interval_seconds(r["expr"])
                if detik is None:
                    kena.append(r)  # server yang evaluasi
                    continue
                if not r["last_fired"]:
                    kena.append(r)
                    continue
                try:
                    lalu = datetime.strptime(str(r["last_fired"]), _NOW).timestamp()
                except ValueError:
                    kena.append(r)
                    continue
                if time.time() - lalu >= detik:
                    kena.append(r)
            rows = kena
        return rows

    def mark_fired(self, schedule_id, when=None):
        """Catat jadwal sudah terbakar (last_fired). True kalau id-nya ada."""
        cur = self._exec("UPDATE schedules SET last_fired = ? WHERE id = ?",
                         (str(when) if when is not None else iso_now(), schedule_id))
        return cur.rowcount > 0

    def delete_schedule(self, schedule_id):
        """Hapus jadwal. True kalau ada baris yang terbuang."""
        return self._exec("DELETE FROM schedules WHERE id = ?", (schedule_id,)).rowcount > 0

    # ── outbox ───────────────────────────────────────────────────
    def enqueue(self, token, chat_id, text, now=None):
        """Masukkan pesan ke antrian kirim. Idempoten: entri pending yang sama
        (token, chat_id, text) tidak diduplikat — UPSERT, balikin id yang ada.
        Pesan siap kirim langsung (next_try = now)."""
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
        """Klaim pesan pending yang next_try-nya sudah lewat:
        attempts+1 dan next_try mundur (backoff, attempt n → now + min(300, 2**n)).
        Balikin pesan yang di-claim (at-least-once); final lewat mark_sent/mark_dead.
        """
        t = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM outbox WHERE status = 'pending' AND next_try <= ?"
                " ORDER BY id" + (f" LIMIT {int(limit)}" if limit else ""),
                (float(t),)).fetchall()
            kena = []
            for r in rows:
                percobaan = r["attempts"] + 1
                self._db.execute(
                    "UPDATE outbox SET attempts = ?, next_try = ? WHERE id = ?",
                    (percobaan, float(t) + min(MAX_BACKOFF, 2 ** percobaan), r["id"]))
                kena.append(dict(r))
            self._db.commit()
            return kena

    def mark_sent(self, outbox_id):
        """pending → sent. Idempoten: baris yang bukan pending tidak diubah."""
        return self._exec("UPDATE outbox SET status = 'sent' WHERE id = ?"
                          " AND status = 'pending'", (outbox_id,)).rowcount > 0

    def mark_dead(self, outbox_id):
        """pending → dead (dead-letter setelah retry habis). Idempoten."""
        return self._exec("UPDATE outbox SET status = 'dead' WHERE id = ?"
                          " AND status = 'pending'", (outbox_id,)).rowcount > 0

    def outbox_count(self, status=None):
        q, p = [], []
        if status is not None:
            q.append("status = ?"); p.append(status)
        sql = "SELECT COUNT(*) AS n FROM outbox" + (" WHERE " + " AND ".join(q) if q else "")
        with self._lock:
            return self._db.execute(sql, p).fetchone()["n"]

    # ── seen (offset getUpdates) ─────────────────────────────────
    def set_offset(self, bot_id, update_id):
        """Simpan offset getUpdates terakhir per bot. UPSERT idempoten."""
        self._exec(
            "INSERT INTO seen(bot_id, last_update_id) VALUES(?,?)"
            " ON CONFLICT(bot_id) DO UPDATE SET last_update_id = excluded.last_update_id",
            (str(bot_id), int(update_id)))

    def get_offset(self, bot_id):
        """Offset terakhir bot; 0 kalau belum pernah disimpan."""
        with self._lock:
            r = self._db.execute("SELECT last_update_id FROM seen WHERE bot_id = ?",
                                 (str(bot_id),)).fetchone()
        return int(r["last_update_id"]) if r else 0

    # ── usage ────────────────────────────────────────────────────
    def add_usage(self, bot_id, day, calls=0, cost=0.0, tokens=0):
        """Tally pemakaian per bot per hari. UPSERT: nilai akumulasi (increment)."""
        self._exec(
            "INSERT INTO usage(bot_id, day, calls, cost, tokens) VALUES(?,?,?,?,?)"
            " ON CONFLICT(bot_id, day) DO UPDATE SET"
            " calls = calls + excluded.calls,"
            " cost = cost + excluded.cost,"
            " tokens = tokens + excluded.tokens",
            (str(bot_id), str(day), int(calls), float(cost), int(tokens)))

    def get_usage(self, bot_id, day=None):
        """Pemakaian bot: dict hari tertentu (default nol) atau agregat semua hari."""
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

    # ── chats (daftar chat yang diketahui bridge) ────────────────
    def save_chat(self, chat_id, judul, ts=None):
        """Catat/upsert judul chat. chat_id disimpan sebagai string (JSON aman)."""
        self._exec(
            "INSERT INTO chats(chat_id, judul, updated_at) VALUES(?,?,?)"
            " ON CONFLICT(chat_id) DO UPDATE SET"
            " judul = excluded.judul, updated_at = excluded.updated_at",
            (str(chat_id), str(judul), iso_now(ts)))

    def list_chats(self):
        """Semua chat, terbaru duluan (rowid sebagai pemencet seri detik)."""
        return self._query(
            "SELECT chat_id, judul, updated_at FROM chats ORDER BY updated_at DESC, rowid DESC")

    # ── personas (kepribadian per nama) ─────────────────────────
    def upsert_persona(self, nama, persona, ts=None):
        if not nama or not persona:
            raise ValueError("nama dan persona wajib diisi")
        kini = iso_now(ts)
        self._exec(
            "INSERT INTO personas(nama, persona, created_at, updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(nama) DO UPDATE SET"
            " persona = excluded.persona, updated_at = excluded.updated_at",
            (str(nama), str(persona), kini, kini))

    def list_personas(self):
        """Semua persona terdaftar urut nama (dict: nama, persona, created_at, updated_at)."""
        return self._query(
            "SELECT nama, persona, created_at, updated_at FROM personas ORDER BY nama")

    def hapus_persona(self, nama):
        """Buang persona. Arsip TIDAK dilakukan di sini — pemanggil (bridge) yang menulis jejak."""
        cur = self._exec("DELETE FROM personas WHERE nama = ?", (str(nama),))
        return cur.rowcount > 0
