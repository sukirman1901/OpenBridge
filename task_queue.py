#!/usr/bin/env python3
"""
TaskQueue — antrean tugas otonom per bot (siklus: goal → plan → act → verify
→ report, langkah terbatas). Tabel BROU tambahan `tasks` di database bridge:
CREATE TABLE IF NOT EXISTS, tidak menyentuh tabel lama (schedules, outbox, dll).

Status tugas: 'antre' → 'jalan' → 'selesai' | 'gagal'.
Bridge klaim tugas antre paling tua (claim_next), jalankan siklus langkah
demi langkah (bump tiap langkah; habis kuota → gagal), lalu tutup lewat
selesaikan/gagalkan. Atomic per operasi: satu koneksi check_same_thread=False
+ threading.Lock, tiap call = satu transaksi (execute + commit di bawah lock).
"""
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

STATUS = ("antre", "jalan", "selesai", "gagal")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY,
    bot_id     TEXT NOT NULL,
    chat_id    INTEGER NOT NULL,
    goal       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'antre'
               CHECK(status IN ('antre','jalan','selesai','gagal')),
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

# Konstanta UTC+7 (WIB) — tidak ikut zona sistem, sama gaya bridge.
_TZ7 = timezone(timedelta(hours=7))


def iso_from_ts(ts=None):
    """Detik epoch → 'YYYY-MM-DD HH:MM:SS' di UTC+7 konstanta."""
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), _TZ7)
    return dt.strftime(_NOW)


class TaskQueue:
    """Antrean tugas otonom per bot. path=None → database :memory:."""

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

    # ── antrean tugas ────────────────────────────────────────────
    def add_task(self, bot_id, chat_id, goal, max_steps=5, now=None):
        """Masukkan tugas baru (status 'antre', steps=0), balikin id-nya."""
        t = iso_from_ts(now)
        cur = self._exec(
            "INSERT INTO tasks(bot_id, chat_id, goal, status, steps, max_steps,"
            " created_at, updated_at) VALUES(?,?,?,'antre',0,?,?,?)",
            (str(bot_id), int(chat_id), str(goal), int(max_steps), t, t))
        return cur.lastrowid

    def claim_next(self, bot_id):
        """Klaim tugas 'antre' paling tua milik bot → status 'jalan' (atomic,
        satu transaksi: SELECT lalu UPDATE di bawah lock). None kalau kosong."""
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM tasks WHERE bot_id = ? AND status = 'antre'"
                " ORDER BY id LIMIT 1", (str(bot_id),)).fetchone()
            if r is None:
                self._db.commit()
                return None
            self._db.execute(
                "UPDATE tasks SET status = 'jalan', updated_at = ? WHERE id = ?",
                (iso_from_ts(), r["id"]))
            self._db.commit()
            kini = dict(r)
            kini["status"] = "jalan"
            return kini

    def bump(self, task_id):
        """Naikkan penghitung langkah tugas yang sedang 'jalan'.
        - 'ok'    : steps+1 masih < max_steps (sisa langkah tersedia).
        - 'penuh' : langkah berikutnya mencapai maks → status langsung 'gagal'
                    dengan last_error='melebihi max_steps' (bridge berhenti
                    mengulang); juga stabil untuk tugas yang sudah final.
        - 'hilang': task_id tak dikenal.
        """
        with self._lock:
            r = self._db.execute("SELECT * FROM tasks WHERE id = ?",
                                 (task_id,)).fetchone()
            if r is None:
                self._db.commit()
                return "hilang"
            if r["status"] in ("selesai", "gagal"):
                # Sudah final — jangan diubah lagi, anggap penuh agar berhenti.
                self._db.commit()
                return "penuh"
            langkah = int(r["steps"]) + 1
            updated = iso_from_ts()
            if langkah < int(r["max_steps"]):
                self._db.execute(
                    "UPDATE tasks SET steps = ?, updated_at = ? WHERE id = ?",
                    (langkah, updated, task_id))
                self._db.commit()
                return "ok"
            self._db.execute(
                "UPDATE tasks SET steps = ?, status = 'gagal',"
                " last_error = 'melebihi max_steps', updated_at = ? WHERE id = ?",
                (langkah, updated, task_id))
            self._db.commit()
            return "penuh"

    def selesaikan(self, task_id, result):
        """'jalan' (atau masih 'antre') → 'selesai', simpan result. Idempoten:
        tugas yang sudah selesai/gagal tidak diubah. True kalau baru ditutup."""
        cur = self._exec(
            "UPDATE tasks SET status = 'selesai', result = ?, updated_at = ?"
            " WHERE id = ? AND status NOT IN ('selesai','gagal')",
            ("" if result is None else str(result), iso_from_ts(), task_id))
        return cur.rowcount > 0

    def gagalkan(self, task_id, err):
        """'jalan' (atau masih 'antre') → 'gagal', simpan last_error. Idempoten."""
        cur = self._exec(
            "UPDATE tasks SET status = 'gagal', last_error = ?, updated_at = ?"
            " WHERE id = ? AND status NOT IN ('selesai','gagal')",
            ("" if err is None else str(err), iso_from_ts(), task_id))
        return cur.rowcount > 0

    def get(self, task_id):
        """Satu tugas (dict semua kolom) atau None kalau tak ada."""
        with self._lock:
            r = self._db.execute("SELECT * FROM tasks WHERE id = ?",
                                 (task_id,)).fetchone()
        return dict(r) if r else None

    def list_tasks(self, bot_id=None, status=None, limit=50):
        """Daftar tugas terbaru dulu (opsional filter bot_id dan/atau status)."""
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
        """Jumlah tugas, bisa difilter status dan/atau bot_id."""
        q, p = [], []
        if status is not None:
            q.append("status = ?"); p.append(status)
        if bot_id is not None:
            q.append("bot_id = ?"); p.append(str(bot_id))
        sql = "SELECT COUNT(*) AS n FROM tasks" + (" WHERE " + " AND ".join(q) if q else "")
        with self._lock:
            return self._db.execute(sql, p).fetchone()["n"]
