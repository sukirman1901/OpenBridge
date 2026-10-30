# unittest untuk rate_limiter.py — pembatas laju per menit per bot
# Dilarang pakai sleep: jendela digeser dengan mock time.time.
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import rate_limiter
from rate_limiter import RateLimiter


class TestRateLimiter(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="limit-")
        self.path = os.path.join(self.dir, "rate.json")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_1_limit_tepat(self):
        """Maks 3: tiga allow pertama True, ke-4 False."""
        rl = RateLimiter(max_per_menit=3)
        self.assertTrue(rl.allow("botA"))
        self.assertTrue(rl.allow("botA"))
        self.assertTrue(rl.allow("botA"))
        self.assertFalse(rl.allow("botA"))
        self.assertEqual(rl.used_last_minute("botA"), 3)

    def test_2_batch_all_or_nothing(self):
        """n=2 mengisi 2 slot; batch yang melewati batas ditolak & tidak mencatat."""
        rl = RateLimiter(max_per_menit=3)
        self.assertTrue(rl.allow("botA", n=2))
        self.assertEqual(rl.used_last_minute("botA"), 2)
        # 2 terisi + 2 kandidat = 4 > 3 → tolak, tetap 2
        self.assertFalse(rl.allow("botA", n=2))
        self.assertEqual(rl.used_last_minute("botA"), 2)
        # sisa 1 slot: pas mengisi → boleh
        self.assertTrue(rl.allow("botA", n=1))
        self.assertEqual(rl.used_last_minute("botA"), 3)
        self.assertFalse(rl.allow("botA", n=1))

    def test_3_jendela_geser_bebas_sleep(self):
        """61 dtk berlalu (mock time) → slot bebas lagi, tanpa sleep."""
        rl = RateLimiter(max_per_menit=1)
        self.assertTrue(rl.allow("botA"))
        self.assertFalse(rl.allow("botA"))
        # waktu bergulir 61 detik (jam dimonyet, bukan sleep)
        dengan_kini = __import__("time").time()
        with patch("rate_limiter.time") as mtime:
            mtime.time.return_value = dengan_kini + 61
            self.assertTrue(rl.allow("botA"))           # jendela sudah kosong
            self.assertFalse(rl.allow("botA"))          # slot baru terpakai satu
        self.assertEqual(rl.used_last_minute("botA"), 1)

    def test_4_max_none_tak_berbatas(self):
        """max None = ∞, tapi tetap mencatat."""
        rl = RateLimiter()
        for _ in range(500):
            self.assertTrue(rl.allow("botA"))
        self.assertEqual(rl.used_last_minute("botA"), 500)
        self.assertTrue(rl.allow("botA", n=250))
        self.assertEqual(rl.used_last_minute("botA"), 750)

    def test_5_isolasi_antar_bot(self):
        """Limit bot A tidak mengganggu bot B."""
        rl = RateLimiter(max_per_menit=2)
        self.assertTrue(rl.allow("botA"))
        self.assertTrue(rl.allow("botA"))
        self.assertFalse(rl.allow("botA"))
        # bot B: jendelanya sendiri, masih lega — dua slotnya ikut terisi
        self.assertTrue(rl.allow("botB"))
        self.assertTrue(rl.allow("botB"))
        self.assertFalse(rl.allow("botB"))
        # A dan B memang terpisah: masing-masing penuh di slotnya sendiri
        kini = __import__("time").time()
        self.assertEqual(len(rl._antrian("botA", kini)), 2)
        self.assertEqual(len(rl._antrian("botB", kini)), 2)

    def test_6_used_last_minute_memotong_lama(self):
        """Hitungan benar + entri keluar jendela otomatis terbuang."""
        rl = RateLimiter(max_per_menit=10)
        rl.allow("botA")
        rl.allow("botA", n=2)
        self.assertEqual(rl.used_last_minute("botA"), 3)
        # mundurkan waktu: sebagian timestamp jadi basi
        lama = __import__("time").time()
        with patch("rate_limiter.time") as mtime:
            mtime.time.return_value = lama
            rl._riwayat["botA"] = [lama - 70, lama - 65, lama - 30, lama - 1]
            self.assertEqual(rl.used_last_minute("botA"), 2)  # basi ikut terprune

    def test_7_persist_roundtrip_dan_prune_load(self):
        """Simpan → muat ulang; timestamp basi dibuang saat load."""
        rl1 = RateLimiter(max_per_menit=5, path=self.path)
        rl1.allow("botA", n=2)
        rl1.allow("botB")
        # tumpukkan riwayat basi manual, lalu simpan
        lama = __import__("time").time()
        rl1._riwayat["botC"] = [lama - 500, lama - 400]
        rl1._save()
        # isi file: botA 2 ts, botB 1, botC 2 (semua basi)
        with open(self.path) as f:
            isi = json.load(f)
        self.assertGreaterEqual(len(isi["botA"]), 2)

        rl2 = RateLimiter(max_per_menit=5, path=self.path)
        self.assertEqual(rl2.used_last_minute("botA"), 2)
        self.assertEqual(rl2.used_last_minute("botB"), 1)
        self.assertEqual(rl2.used_last_minute("botC"), 0)  # basi terprune saat load
        self.assertTrue(rl2.allow("botC"))                 # slot botC lega lagi

    def test_8_file_rusak_mulai_bersih(self):
        """JSON invalid → mulai bersih, tanpa exception."""
        with open(self.path, "w") as f:
            f.write("{ini bukan json!!!")
        rl = RateLimiter(max_per_menit=3, path=self.path)  # tidak boleh exception
        self.assertEqual(rl.used_last_minute("botA"), 0)
        self.assertTrue(rl.allow("botA"))
        # data baru tetap bisa ditulis & dibaca ulang
        rl2 = RateLimiter(path=self.path)
        self.assertEqual(rl2.used_last_minute("botA"), 1)

    def test_9_reset_satu_dan_semua(self):
        """reset(bot) hapus satu; reset() kosongkan semua."""
        rl = RateLimiter(max_per_menit=1, path=self.path)
        rl.allow("botA")
        rl.allow("botB")
        rl.reset("botA")
        self.assertEqual(rl.used_last_minute("botA"), 0)
        self.assertEqual(rl.used_last_minute("botB"), 1)
        self.assertTrue(rl.allow("botA"))  # slot botA bebas lagi
        rl.reset()
        self.assertEqual(rl.used_last_minute("botA"), 0)
        self.assertEqual(rl.used_last_minute("botB"), 0)
        # persist setelah reset: file bersih
        rl2 = RateLimiter(path=self.path)
        self.assertEqual(rl2.used_last_minute("botA"), 0)


if __name__ == "__main__":
    unittest.main()
