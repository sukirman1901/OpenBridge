# unittest for rate_limiter.py — per-minute, per-bot rate limiter
# No sleeping allowed: the window is shifted with a mocked time.time.
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
        """Max 3: the first three allow calls are True, the 4th is False."""
        rl = RateLimiter(max_per_menit=3)
        self.assertTrue(rl.allow("botA"))
        self.assertTrue(rl.allow("botA"))
        self.assertTrue(rl.allow("botA"))
        self.assertFalse(rl.allow("botA"))
        self.assertEqual(rl.used_last_minute("botA"), 3)

    def test_2_batch_all_or_nothing(self):
        """n=2 fills 2 slots; a batch that would exceed the limit is rejected & records nothing."""
        rl = RateLimiter(max_per_menit=3)
        self.assertTrue(rl.allow("botA", n=2))
        self.assertEqual(rl.used_last_minute("botA"), 2)
        # 2 used + 2 candidates = 4 > 3 → reject, still 2
        self.assertFalse(rl.allow("botA", n=2))
        self.assertEqual(rl.used_last_minute("botA"), 2)
        # 1 slot left: exactly filling it is allowed
        self.assertTrue(rl.allow("botA", n=1))
        self.assertEqual(rl.used_last_minute("botA"), 3)
        self.assertFalse(rl.allow("botA", n=1))

    def test_3_jendela_geser_bebas_sleep(self):
        """61 seconds elapsed (mocked clock) → slots free again, without sleep."""
        rl = RateLimiter(max_per_menit=1)
        self.assertTrue(rl.allow("botA"))
        self.assertFalse(rl.allow("botA"))
        # time rolls forward 61 seconds (mocked clock, no sleep)
        dengan_kini = __import__("time").time()
        with patch("rate_limiter.time") as mtime:
            mtime.time.return_value = dengan_kini + 61
            self.assertTrue(rl.allow("botA"))           # window is already empty
            self.assertFalse(rl.allow("botA"))          # one new slot used up
        self.assertEqual(rl.used_last_minute("botA"), 1)

    def test_4_max_none_tak_berbatas(self):
        """max None = ∞, but still recorded."""
        rl = RateLimiter()
        for _ in range(500):
            self.assertTrue(rl.allow("botA"))
        self.assertEqual(rl.used_last_minute("botA"), 500)
        self.assertTrue(rl.allow("botA", n=250))
        self.assertEqual(rl.used_last_minute("botA"), 750)

    def test_5_isolasi_antar_bot(self):
        """Bot A's limit does not affect bot B."""
        rl = RateLimiter(max_per_menit=2)
        self.assertTrue(rl.allow("botA"))
        self.assertTrue(rl.allow("botA"))
        self.assertFalse(rl.allow("botA"))
        # bot B: its own window, still roomy — both of its slots get filled
        self.assertTrue(rl.allow("botB"))
        self.assertTrue(rl.allow("botB"))
        self.assertFalse(rl.allow("botB"))
        # A and B are truly separate: each is full in its own slots
        now = __import__("time").time()
        self.assertEqual(len(rl._queue("botA", now)), 2)
        self.assertEqual(len(rl._queue("botB", now)), 2)

    def test_6_used_last_minute_memotong_lama(self):
        """Correct counts + entries leaving the window are dropped automatically."""
        rl = RateLimiter(max_per_menit=10)
        rl.allow("botA")
        rl.allow("botA", n=2)
        self.assertEqual(rl.used_last_minute("botA"), 3)
        # rewind the clock: some timestamps become stale
        lama = __import__("time").time()
        with patch("rate_limiter.time") as mtime:
            mtime.time.return_value = lama
            rl._riwayat["botA"] = [lama - 70, lama - 65, lama - 30, lama - 1]
            self.assertEqual(rl.used_last_minute("botA"), 2)  # stale entries pruned too

    def test_7_persist_roundtrip_dan_prune_load(self):
        """Save → reload; stale timestamps are discarded on load."""
        rl1 = RateLimiter(max_per_menit=5, path=self.path)
        rl1.allow("botA", n=2)
        rl1.allow("botB")
        # inject stale history manually, then save
        lama = __import__("time").time()
        rl1._riwayat["botC"] = [lama - 500, lama - 400]
        rl1._save()
        # file contents: botA 2 ts, botB 1, botC 2 (all stale)
        with open(self.path) as f:
            isi = json.load(f)
        self.assertGreaterEqual(len(isi["botA"]), 2)

        rl2 = RateLimiter(max_per_menit=5, path=self.path)
        self.assertEqual(rl2.used_last_minute("botA"), 2)
        self.assertEqual(rl2.used_last_minute("botB"), 1)
        self.assertEqual(rl2.used_last_minute("botC"), 0)  # stale pruned on load
        self.assertTrue(rl2.allow("botC"))                 # botC's roomy slot again

    def test_8_file_rusak_mulai_bersih(self):
        """Invalid JSON → start clean, no exception."""
        with open(self.path, "w") as f:
            f.write("{this is not json!!!")
        rl = RateLimiter(max_per_menit=3, path=self.path)  # must not raise
        self.assertEqual(rl.used_last_minute("botA"), 0)
        self.assertTrue(rl.allow("botA"))
        # new data must still be writeable & readable again
        rl2 = RateLimiter(path=self.path)
        self.assertEqual(rl2.used_last_minute("botA"), 1)

    def test_9_reset_satu_dan_semua(self):
        """reset(bot) clears one; reset() clears everything."""
        rl = RateLimiter(max_per_menit=1, path=self.path)
        rl.allow("botA")
        rl.allow("botB")
        rl.reset("botA")
        self.assertEqual(rl.used_last_minute("botA"), 0)
        self.assertEqual(rl.used_last_minute("botB"), 1)
        self.assertTrue(rl.allow("botA"))  # botA's slot free again
        rl.reset()
        self.assertEqual(rl.used_last_minute("botA"), 0)
        self.assertEqual(rl.used_last_minute("botB"), 0)
        # persist after reset: the file is clean
        rl2 = RateLimiter(path=self.path)
        self.assertEqual(rl2.used_last_minute("botA"), 0)


if __name__ == "__main__":
    unittest.main()
