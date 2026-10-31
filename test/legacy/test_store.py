#!/usr/bin/env python3
"""
Token test untuk store.py — jalankan: python3 -m unittest -v test_store.py
Pakai file db sementara di /tmp (pastikan dibersihkan di tearDown).
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

    def test_wal_dan_pragma_aktif(self):
        mode = self.s._db.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")
        timeout = self.s._db.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(timeout, 5000)

    def test_tabel_dan_skema_lengkap(self):
        for tabel, kolom in (("schedules", "tz"), ("outbox", "next_try"),
                             ("seen", "last_update_id"), ("usage", "tokens")):
            cols = {r["name"] for r in self.s._db.execute(f"PRAGMA table_info({tabel})")}
            self.assertIn(kolom, cols, f"kolom {kolom} hilang di {tabel}")

    def test_add_clock_due_sekali_lalu_tandai_terbakar(self):
        sid = self.s.add_schedule("b1", 123, "2026-01-01 10:00", "once", "sapa")
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

    def test_delete_schedule_menghilangkan_baris(self):
        sid = self.s.add_schedule("b2", 5, "08:00", "cron", "x")
        self.assertTrue(self.s.delete_schedule(sid))
        self.assertFalse(self.s.delete_schedule(sid))
        self.assertEqual(self.s.due_schedules(bot_id="b2"), [])
        with self.assertRaises(ValueError):
            self.s.add_schedule("b2", 5, "08:00", "taksah", "x")

    def test_enqueue_upsert_idempoten(self):
        id1 = self.s.enqueue("TOK", 111, "hai")
        id2 = self.s.enqueue("TOK", 111, "hai")     # sama → tak diduplikat
        id3 = self.s.enqueue("TOK", 111, "hai lagi")
        self.assertEqual(id1, id2)
        self.assertNotEqual(id1, id3)
        self.assertEqual(self.s.outbox_count(status="pending"), 2)

    def test_due_outbox_hanya_pending_dan_jatuh_tempo(self):
        kini = time.time()
        terlambat = self.s.enqueue("TOK", 1, "lama", now=kini - 100)
        kedepan = self.s.enqueue("TOK", 2, "masa depan", now=kini)
        self.s._exec("UPDATE outbox SET next_try = ? WHERE id = ?", (kini + 500, kedepan))
        kena = self.s.due_outbox(now=kini)
        self.assertEqual([r["id"] for r in kena], [terlambat])
        # diklaim → bukan pending lagi utk klaim berikutnya pada waktu sama
        self.assertEqual(self.s.due_outbox(now=kini), [])
        # sudah sent → tak muncul lagi, sekalipun next_try-nya lewat
        self.assertTrue(self.s.mark_sent(terlambat))
        self.s._exec("UPDATE outbox SET next_try = ? WHERE id = ?", (kini, terlambat))
        self.assertNotIn(terlambat, [r["id"] for r in self.s.due_outbox(now=kini + 1000)])
        # yang jatuh tempo murni baris kedepan (status masih pending)
        self.assertEqual([r["id"] for r in self.s.due_outbox(now=kini + 1000)], [])
        self.assertEqual(self.s.outbox_count(status="sent"), 1)

    def test_mark_sent_idempoten_tak_rusak_datau(self):
        oid = self.s.enqueue("TOK", 7, "pesanku", now=1000.0)
        self.s.due_outbox(now=1000.0)                       # attempts → 1
        self.assertTrue(self.s.mark_sent(oid))
        before = self.s._query("SELECT * FROM outbox WHERE id = ?", (oid,))[0]
        self.assertFalse(self.s.mark_sent(oid))             # klaim kedua → False
        after = self.s._query("SELECT * FROM outbox WHERE id = ?", (oid,))[0]
        self.assertEqual(before, after)                     # attempts & status utuh
        self.assertEqual(after["status"], "sent")

    def test_backoff_naik_tiap_klaim(self):
        oid = self.s.enqueue("TOK", 9, "retry", now=0.0)
        self.s.due_outbox(now=100.0)                        # klaim 1: tunggu 2^1 = 2
        r1 = self.s._query("SELECT attempts, next_try FROM outbox WHERE id = ?", (oid,))[0]
        self.assertEqual(r1["attempts"], 1)
        self.assertAlmostEqual(r1["next_try"], 102.0)
        self.s.due_outbox(now=102.5)                        # klaim 2: tunggu 2^2 = 4
        r2 = self.s._query("SELECT attempts, next_try FROM outbox WHERE id = ?", (oid,))[0]
        self.assertEqual(r2["attempts"], 2)
        self.assertAlmostEqual(r2["next_try"], 106.5)
        self.s.due_outbox(now=106.6)                        # klaim 3: tunggu 2^3 = 8
        r3 = self.s._query("SELECT attempts, next_try FROM outbox WHERE id = ?", (oid,))[0]
        self.assertAlmostEqual(r3["next_try"], 114.6)
        self.assertGreater(r2["next_try"], r1["next_try"])
        self.assertGreater(r3["next_try"], r2["next_try"])

    def test_backoff_dibatasi_300_detik(self):
        oid = self.s.enqueue("TOK", 10, "lambat", now=0.0)
        self.s._exec("UPDATE outbox SET attempts = 15 WHERE id = ?", (oid,))
        self.s.due_outbox(now=1000.0)
        r = self.s._query("SELECT attempts, next_try FROM outbox WHERE id = ?", (oid,))[0]
        self.assertEqual(r["attempts"], 16)
        self.assertAlmostEqual(r["next_try"], 1000.0 + 300)  # 2**16 → di-cap 300

    def test_mark_dead_menghentikan_retry(self):
        oid = self.s.enqueue("TOK", 11, "mati", now=0.0)
        self.assertTrue(self.s.mark_dead(oid))
        self.assertFalse(self.s.mark_dead(oid))             # idempoten
        self.assertEqual(self.s.due_outbox(now=time.time() + 9999), [])
        self.assertEqual(self.s.outbox_count(status="dead"), 1)

    def test_offset_persist_bertahan_restart(self):
        self.s.set_offset("bA", 120)
        self.assertEqual(self.s.get_offset("bA"), 120)
        self.s.set_offset("bA", 456)                        # maju
        self.s.set_offset("bB", 98)
        self.assertEqual(self.s.get_offset("bA"), 456)
        self.assertEqual(self.s.get_offset("bB"), 98)
        self.assertEqual(self.s.get_offset("bC"), 0)        # bot tak dikenal
        # buka ulang db dari disk → offset masih ada (persist)
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
