#!/usr/bin/env python3
"""
Unit tests for store.py — run: python3 -m unittest -v test_store.py
Uses a temporary db file in /tmp (make sure it is cleaned up in tearDown).
"""
import os
import shutil
import tempfile
import time
import unittest

import store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="store_test_")
        self.path = os.path.join(self.dir, "test.db")
        self.s = store.Store(self.path)

    def tearDown(self):
        self.s.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_wal_and_pragma_on(self):
        mode = self.s._db.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")
        timeout = self.s._db.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(timeout, 5000)

    def test_tables_and_schema_complete(self):
        for tabel, kolom in (("schedules", "tz"), ("outbox", "next_try"),
                             ("seen", "last_update_id"), ("usage", "tokens")):
            cols = {r["name"] for r in self.s._db.execute(f"PRAGMA table_info({tabel})")}
            self.assertIn(kolom, cols, f"column {kolom} missing in {tabel}")

    def test_add_clock_due_fires_once_then_marks_burned(self):
        sid = self.s.add_schedule("b1", 123, "2026-01-01 10:00", "once", "greet")
        due = self.s.due_schedules(bot_id="b1", kind="once", now="2026-01-01 10:01")
        self.assertEqual([r["id"] for r in due], [sid])
        self.assertTrue(self.s.mark_fired(sid))
        due_lagi = self.s.due_schedules(bot_id="b1", kind="once", now="2026-01-01 10:02")
        self.assertEqual(due_lagi, [])

    def test_due_filter_jenis_jadwal(self):
        c1 = self.s.add_schedule("b1", 1, "21:00", "cron", "a")
        i1 = self.s.add_schedule("b1", 2, "3600", "interval", "b")
        o1 = self.s.add_schedule("b1", 3, "2026-01-01 09:00", "once", "c")
        kind_cron = {r["id"] for r in self.s.due_schedules(bot_id="b1", kind="cron")}
        self.assertEqual(kind_cron, {c1})
        self.assertNotIn(o1, kind_cron)
        lewat = {r["id"] for r in self.s.due_schedules(bot_id="b1", kind="once",
                                                       now="2026-01-02 00:00")}
        self.assertEqual(lewat, {o1})
        intra = {r["id"] for r in self.s.due_schedules(bot_id="b1", kind="interval")}
        self.assertEqual(intra, {i1})
        semua = {r["id"] for r in self.s.due_schedules()}
        self.assertEqual(semua, {c1, i1, o1})

    def test_delete_schedule_removes_row(self):
        sid = self.s.add_schedule("b2", 5, "08:00", "cron", "x")
        self.assertTrue(self.s.delete_schedule(sid))
        self.assertFalse(self.s.delete_schedule(sid))
        self.assertEqual(self.s.due_schedules(bot_id="b2"), [])
        with self.assertRaises(ValueError):
            self.s.add_schedule("b2", 5, "08:00", "bogus", "x")

    def test_enqueue_upsert_idempoten(self):
        id1 = self.s.enqueue("TOK", 111, "hi")
        id2 = self.s.enqueue("TOK", 111, "hi")      # same → not duplicated
        id3 = self.s.enqueue("TOK", 111, "hi again")
        self.assertEqual(id1, id2)
        self.assertNotEqual(id1, id3)
        self.assertEqual(self.s.outbox_count(status="pending"), 2)

    def test_due_outbox_only_pending_and_due(self):
        now = time.time()
        late = self.s.enqueue("TOK", 1, "old", now=now - 100)
        soon = self.s.enqueue("TOK", 2, "future", now=now)
        self.s._exec("UPDATE outbox SET next_try = ? WHERE id = ?", (now + 500, soon))
        hit = self.s.due_outbox(now=now)
        self.assertEqual([r["id"] for r in hit], [late])
        # claimed → no longer pending for the next claim at the same instant
        self.assertEqual(self.s.due_outbox(now=now), [])
        # already sent → never shows up again, even if next_try has passed
        self.assertTrue(self.s.mark_sent(late))
        self.s._exec("UPDATE outbox SET next_try = ? WHERE id = ?", (now, late))
        self.assertNotIn(late, [r["id"] for r in self.s.due_outbox(now=now + 1000)])
        # the only row due at that time is "future" (still pending) — and it is not due yet
        self.assertEqual([r["id"] for r in self.s.due_outbox(now=now + 1000)], [])
        self.assertEqual(self.s.outbox_count(status="sent"), 1)

    def test_mark_sent_idempoten_tak_rusak_datau(self):
        oid = self.s.enqueue("TOK", 7, "my message", now=1000.0)
        self.s.due_outbox(now=1000.0)                       # attempts → 1
        self.assertTrue(self.s.mark_sent(oid))
        before = self.s._query("SELECT * FROM outbox WHERE id = ?", (oid,))[0]
        self.assertFalse(self.s.mark_sent(oid))             # second claim → False
        after = self.s._query("SELECT * FROM outbox WHERE id = ?", (oid,))[0]
        self.assertEqual(before, after)                     # attempts & status intact
        self.assertEqual(after["status"], "sent")

    def test_backoff_grows_per_claim(self):
        oid = self.s.enqueue("TOK", 9, "retry", now=0.0)
        self.s.due_outbox(now=100.0)                        # claim 1: wait 2^1 = 2
        r1 = self.s._query("SELECT attempts, next_try FROM outbox WHERE id = ?", (oid,))[0]
        self.assertEqual(r1["attempts"], 1)
        self.assertAlmostEqual(r1["next_try"], 102.0)
        self.s.due_outbox(now=102.5)                        # claim 2: wait 2^2 = 4
        r2 = self.s._query("SELECT attempts, next_try FROM outbox WHERE id = ?", (oid,))[0]
        self.assertEqual(r2["attempts"], 2)
        self.assertAlmostEqual(r2["next_try"], 106.5)
        self.s.due_outbox(now=106.6)                        # claim 3: wait 2^3 = 8
        r3 = self.s._query("SELECT attempts, next_try FROM outbox WHERE id = ?", (oid,))[0]
        self.assertAlmostEqual(r3["next_try"], 114.6)
        self.assertGreater(r2["next_try"], r1["next_try"])
        self.assertGreater(r3["next_try"], r2["next_try"])

    def test_backoff_is_capped_300_detik(self):
        oid = self.s.enqueue("TOK", 10, "slow", now=0.0)
        self.s._exec("UPDATE outbox SET attempts = 15 WHERE id = ?", (oid,))
        self.s.due_outbox(now=1000.0)
        r = self.s._query("SELECT attempts, next_try FROM outbox WHERE id = ?", (oid,))[0]
        self.assertEqual(r["attempts"], 16)
        self.assertAlmostEqual(r["next_try"], 1000.0 + 300)  # 2**16 → capped at 300

    def test_mark_dead_menghentikan_retry(self):
        oid = self.s.enqueue("TOK", 11, "dead", now=0.0)
        self.assertTrue(self.s.mark_dead(oid))
        self.assertFalse(self.s.mark_dead(oid))             # idempotent
        self.assertEqual(self.s.due_outbox(now=time.time() + 9999), [])
        self.assertEqual(self.s.outbox_count(status="dead"), 1)

    def test_offset_persist_bertahan_restart(self):
        self.s.set_offset("bA", 120)
        self.assertEqual(self.s.get_offset("bA"), 120)
        self.s.set_offset("bA", 456)                        # advanced
        self.s.set_offset("bB", 98)
        self.assertEqual(self.s.get_offset("bA"), 456)
        self.assertEqual(self.s.get_offset("bB"), 98)
        self.assertEqual(self.s.get_offset("bC"), 0)        # unknown bot
        # reopen the db from disk → offsets are still there (persisted)
        self.s.close()
        self.s = store.Store(self.path)
        self.assertEqual(self.s.get_offset("bA"), 456)

    def test_usage_upsert_akumulasi(self):
        self.s.add_usage("b1", "2026-10-30", calls=2, cost=0.5, tokens=100)
        self.s.add_usage("b1", "2026-10-30", calls=3, cost=0.25, tokens=50)
        u = self.s.get_usage("b1", "2026-10-30")
        self.assertEqual(u["calls"], 5)
        self.assertAlmostEqual(u["cost"], 0.75)
        self.assertEqual(u["tokens"], 150)
        self.s.add_usage("b1", "2026-10-31", calls=1)
        agg = self.s.get_usage("b1")
        self.assertEqual(agg["calls"], 6)
        self.assertEqual(self.get_usage_zero("bx"), 0)

    def get_usage_zero(self, bot):
        u = self.s.get_usage(bot, "1970-01-01")
        return u["calls"] + u["tokens"] + u["cost"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
