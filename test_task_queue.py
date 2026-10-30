#!/usr/bin/env python3
"""test_task_queue.py — unittest untuk task_queue.py (antrean tugas otonom)."""
import os
import sqlite3
import tempfile
import unittest

from task_queue import TaskQueue, iso_from_ts


class TestTaskQueue(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.path)          # biar SQLite yang bikin file-nya
        self.list_db = []             # trek file temp untuk cleanup

    def tearDown(self):
        for p in self.list_db:
            for suf in ("", "-wal", "-shm"):
                if os.path.exists(p + suf):
                    os.remove(p + suf)

    def buat_file_db(self):
        self.list_db.append(self.path)
        return self.path

    # 1) add + get ------------------------------------------------
    def test_add_dan_get(self):
        tq = TaskQueue()  # :memory:
        tid = tq.add_task("botA", 111, "cari ringkasan berita", max_steps=3)
        t = tq.get(tid)
        self.assertIsNotNone(t)
        self.assertEqual(t["bot_id"], "botA")
        self.assertEqual(t["chat_id"], 111)
        self.assertEqual(t["goal"], "cari ringkasan berita")
        self.assertEqual(t["status"], "antre")
        self.assertEqual(t["steps"], 0)
        self.assertEqual(t["max_steps"], 3)
        self.assertIsNone(t["result"])
        self.assertIsNone(t["last_error"])
        # kolom waktu terisi format ISO (UTC+7)
        self.assertEqual(t["created_at"], t["updated_at"])
        self.assertEqual(t["created_at"], iso_from_ts())  # ~sekarang
        self.assertIsNone(tq.get(9999))
        tq.close()

    # 2) claim_next: urut antrian & hanya 'antre' -----------------
    def test_claim_next_urut_dan_hanya_antre(self):
        tq = TaskQueue()
        a = tq.add_task("botA", 1, "tugas-A")
        b = tq.add_task("botA", 1, "tugas-B")
        # tugas pertama jadi 'jalan' lewat claim — tidak boleh di-claim lagi
        klaim1 = tq.claim_next("botA")
        self.assertEqual(klaim1["id"], a)
        self.assertEqual(klaim1["status"], "jalan")
        # di DB tetap tercatat 'jalan'
        self.assertEqual(tq.get(a)["status"], "jalan")
        # claim berikutnya: tugas-B (tertua yang masih 'antre')
        klaim2 = tq.claim_next("botA")
        self.assertEqual(klaim2["id"], b)
        self.assertEqual(klaim2["status"], "jalan")
        # habis — antrean kosong
        self.assertIsNone(tq.claim_next("botA"))
        # tugas yang sudah 'gagal' tidak ikut di-claim
        c = tq.add_task("botA", 1, "tugas-C")
        tq.gagalkan(c, "yahoo error")
        self.assertIsNone(tq.claim_next("botA"))
        tq.close()

    def test_claim_next_isolasi_per_bot(self):
        tq = TaskQueue()
        tq.add_task("botA", 1, "milik A")
        self.assertIsNone(tq.claim_next("botB"))          # tidak melintasi bot
        klaim = tq.claim_next("botA")
        self.assertEqual(klaim["goal"], "milik A")
        # partial table: dua TaskQueue di file sama, tabel tetap satu
        # (diuji lokal via :memory: beda instance tetap terpisah — ini file)
        tq.close()

    # 3) max_steps: ok, ok, penuh → gagal -------------------------
    def test_bump_habiskan_langkah_gagal(self):
        tq = TaskQueue()
        tid = tq.add_task("botA", 1, "siklus panjang", max_steps=3)
        tq.claim_next("botA")
        self.assertEqual(tq.bump(tid), "ok")              # 1 < 3
        self.assertEqual(tq.bump(tid), "ok")              # 2 < 3
        self.assertEqual(tq.get(tid)["steps"], 2)
        self.assertEqual(tq.get(tid)["status"], "jalan")
        self.assertEqual(tq.bump(tid), "penuh")           # 3 == maks → gagal
        t = tq.get(tid)
        self.assertEqual(t["status"], "gagal")
        self.assertEqual(t["last_error"], "melebihi max_steps")
        self.assertEqual(t["steps"], 3)
        # setelah gagal, bump tambahan stabil: 'penuh', status tak berubah
        self.assertEqual(tq.bump(tid), "penuh")
        self.assertEqual(tq.get(tid)["status"], "gagal")
        self.assertEqual(tq.get(tid)["steps"], 3)
        # bump task yang tidak ada
        self.assertEqual(tq.bump(424242), "hilang")
        tq.close()

    # 4) selesaikan/gagalkan idempoten ----------------------------
    def test_selesaikan_gagalkan_idempoten(self):
        tq = TaskQueue()
        tid = tq.add_task("botA", 1, "tugas selesai")
        tq.claim_next("botA")
        self.assertTrue(tq.selesaikan(tid, "rekap terkirim"))
        t = tq.get(tid)
        self.assertEqual(t["status"], "selesai")
        self.assertEqual(t["result"], "rekap terkirim")
        # panggil ulang: idempoten, result & status tak berubah
        self.assertFalse(tq.selesaikan(tid, "rekap terkirim lagi"))
        t = tq.get(tid)
        self.assertEqual(t["status"], "selesai")
        self.assertEqual(t["result"], "rekap terkirim")
        # gagalkan di atas 'selesai': ikut idempoten, tak mengubah hasil
        self.assertFalse(tq.gagalkan(tid, "xxx"))
        self.assertEqual(tq.get(tid)["status"], "selesai")
        self.assertIsNone(tq.get(tid)["last_error"])

        tid2 = tq.add_task("botA", 1, "tugas gagal")
        self.assertTrue(tq.gagalkan(tid2, "verifikasi gagal"))
        self.assertEqual(tq.get(tid2)["status"], "gagal")
        self.assertEqual(tq.get(tid2)["last_error"], "verifikasi gagal")
        self.assertFalse(tq.gagalkan(tid2, "ya sudah"))
        self.assertEqual(tq.get(tid2)["last_error"], "verifikasi gagal")
        self.assertFalse(tq.selesaikan(tid2, "jangan"))
        self.assertEqual(tq.get(tid2)["status"], "gagal")
        tq.close()

    # 5) list_tasks: filter & urutan ------------------------------
    def test_list_tasks_filter_dan_urutan(self):
        tq = TaskQueue()
        tq.add_task("botA", 1, "A1")
        tq.add_task("botA", 1, "A2")
        tq.add_task("botB", 2, "B1")
        tq.add_task("botA", 3, "A3")
        # urut terbaru dulu
        semua = tq.list_tasks(limit=50)
        self.assertEqual([t["goal"] for t in semua], ["A3", "B1", "A2", "A1"])
        # filter bot
        self.assertEqual([t["goal"] for t in tq.list_tasks(bot_id="botA")],
                         ["A3", "A2", "A1"])
        # filter status
        b1 = tq.claim_next("botB")
        self.assertEqual([t["goal"] for t in tq.list_tasks(status="jalan")],
                         ["B1"])
        tq.selesaikan(b1["id"], "beres")
        self.assertEqual([t["goal"] for t in tq.list_tasks(status="selesai")],
                         ["B1"])
        # gabungan filter: terbaru dulu hanya botA
        self.assertEqual([t["goal"] for t in tq.list_tasks(bot_id="botA", limit=2)],
                         ["A3", "A2"])
        tq.close()

    # 6) count ----------------------------------------------------
    def test_count(self):
        tq = TaskQueue()
        self.assertEqual(tq.count(), 0)
        a = tq.add_task("botA", 1, "X1")
        tq.add_task("botA", 1, "X2")
        tq.add_task("botB", 2, "Y1")
        self.assertEqual(tq.count(), 3)
        self.assertEqual(tq.count(bot_id="botA"), 2)
        self.assertEqual(tq.count(status="antre"), 3)
        tq.claim_next("botA")
        self.assertEqual(tq.count(status="antre"), 2)
        self.assertEqual(tq.count(status="jalan"), 1)
        tq.selesaikan(a, "ok")
        self.assertEqual(tq.count(status="selesai"), 1)
        self.assertEqual(tq.count(status="antre", bot_id="botB"), 1)
        self.assertEqual(tq.count(status="gagal"), 0)
        tq.close()

    # 7) persist reopen file --------------------------------------
    def test_persist_reopen_file(self):
        path = self.buat_file_db()
        tq = TaskQueue(path)
        tid = tq.add_task("botA", 1, "bertahan restart", max_steps=2)
        b = tq.add_task("botA", 1, "kedua")
        tq.claim_next("botA")                             # tid → 'jalan'
        tq.selesaikan(tid, "sebelum tutup")
        tq.gagalkan(999, "tak ada")                       # no-op
        tq.close()

        tq2 = TaskQueue(path)
        t = tq2.get(tid)
        self.assertIsNotNone(t)
        self.assertEqual(t["status"], "selesai")
        self.assertEqual(t["result"], "sebelum tutup")
        self.assertEqual(t["goal"], "bertahan restart")
        self.assertEqual(t["max_steps"], 2)
        klaim = tq2.claim_next("botA")                    # b masih 'antre'
        self.assertIsNotNone(klaim)
        self.assertEqual(klaim["id"], b)
        # lanjutan siklus di instance baru juga jalan
        self.assertEqual(tq2.bump(b), "ok")
        tq2.close()

    # 8) tabel lama tidak rusak -----------------------------------
    def test_tabel_schedules_tetap_utuh(self):
        path = self.buat_file_db()
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE schedules (id INTEGER PRIMARY KEY, bot_id TEXT,"
            " chat_id INTEGER, expr TEXT, kind TEXT, prompt TEXT,"
            " active INTEGER DEFAULT 1)")
        conn.execute(
            "INSERT INTO schedules(bot_id, chat_id, expr, kind, prompt)"
            " VALUES('lama', 1, '08:30', 'cron', 'jadwal lama')")
        conn.commit()
        conn.close()

        tq = TaskQueue(path)                              # CREATE TABLE IF NOT EXISTS tasks
        tid = tq.add_task("botA", 1, "tugas baru")
        tq.selesaikan(tid, "cocok")
        tq.close()

        conn = sqlite3.connect(path)
        bar = conn.execute(
            "SELECT bot_id, prompt FROM schedules").fetchall()
        conn.close()
        self.assertEqual(bar, [("lama", "jadwal lama")])

    # 9) skenario siklus penuh (goal → plan → act → verify → report)
    def test_siklus_penuh_report(self):
        tq = TaskQueue()
        tid = tq.add_task("botA", 100, "ringkas grup harian", max_steps=5)
        task = tq.claim_next("botA")
        self.assertEqual(task["chat_id"], 100)
        for _ in range(4):                                # plan, act, verify x3
            self.assertEqual(tq.bump(tid), "ok")
        self.assertEqual(tq.get(tid)["steps"], 4)
        self.assertTrue(tq.selesaikan(tid, "rekap 5 langkah terkirim ke chat"))
        t = tq.get(tid)
        self.assertEqual(t["status"], "selesai")
        self.assertIn("terkirim", t["result"])
        self.assertIsNone(t["last_error"])
        self.assertEqual(tq.count(status="selesai"), 1)
        tq.close()

    # ekstra: max_steps tak valid masih bisa dimasukkan, bump langsung penuh
    def test_max_steps_satu_langsung_penuh(self):
        tq = TaskQueue()
        tid = tq.add_task("botA", 1, "satu langkah", max_steps=1)
        tq.claim_next("botA")
        self.assertEqual(tq.bump(tid), "penuh")
        t = tq.get(tid)
        self.assertEqual(t["status"], "gagal")
        self.assertEqual(t["last_error"], "melebihi max_steps")
        tq.close()

    def test_iso_from_ts_utc7(self):
        # 2026-06-12 01:30 UTC → 08:30 WIB
        import calendar
        from datetime import datetime, timedelta
        utc = datetime(2026, 6, 12, 1, 30)
        ts = calendar.timegm(utc.utctimetuple())
        self.assertEqual(iso_from_ts(ts), "2026-06-12 08:30:00")
        self.assertEqual(iso_from_ts(ts + 86400), "2026-06-13 08:30:00")


if __name__ == "__main__":
    unittest.main()
