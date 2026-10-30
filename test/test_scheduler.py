#!/usr/bin/env python3
"""test_scheduler.py — unittest for scheduler.py (schedule parser & matcher)."""
import calendar
import unittest
from datetime import datetime, timedelta

import scheduler
from scheduler import parse, matches, next_run


def epoch_lokal(y, mo, d, h, mi, s=0):
    """A UTC+7 local time of a known value → UTC epoch seconds."""
    utc = datetime(y, mo, d, h, mi, s) - timedelta(hours=7)
    return calendar.timegm(utc.utctimetuple())


DT = datetime          # shorthand

LOCAL_DT = DT(2026, 6, 12, 8, 30)                  # Friday, local UTC+7
EPOCH = epoch_lokal(2026, 6, 12, 8, 30)


class TestLegacy(unittest.TestCase):
    def test_legacy_2100_hanya_minit0_jam21(self):
        p = parse("21:00")
        self.assertEqual(p["kind"], "daily")
        self.assertEqual(p["minute"], {0})
        self.assertEqual(p["hour"], {21})
        # matches exactly at 21:00
        self.assertTrue(matches("21:00", epoch_lokal(2026, 1, 5, 21, 0)))
        # does not match other minutes / hours
        self.assertFalse(matches("21:00", epoch_lokal(2026, 1, 5, 21, 1)))
        self.assertFalse(matches("21:00", epoch_lokal(2026, 1, 5, 20, 59)))
        self.assertFalse(matches("21:00", epoch_lokal(2026, 1, 5, 9, 0)))

    def test_legacy_interval_tiap5_menit_semua_jam(self):
        p = parse("*/5")
        self.assertEqual(p["kind"], "interval")
        self.assertEqual(p["fields"], {"every": 5})
        for jam, menit in ((0, 0), (3, 25), (7, 55), (23, 5)):
            self.assertTrue(matches("*/5", epoch_lokal(2026, 3, 9, jam, menit)))
        self.assertFalse(matches("*/5", epoch_lokal(2026, 3, 9, 7, 3)))
        self.assertFalse(matches("*/5", epoch_lokal(2026, 3, 9, 12, 59)))
        # equivalent to cron "*/5 * * * *"
        self.assertEqual(parse("*/5")["minute"], parse("*/5 * * * *")["minute"])

    def test_legacy_interval_self_not_stateful(self):
        # convention: no global state — repeated calls return identical results
        a = parse("*/10")
        for _ in range(3):
            self.assertEqual(parse("*/10")["minute"], a["minute"])
        self.assertTrue(matches("*/10", epoch_lokal(2026, 2, 1, 0, 0)))


class TestCron(unittest.TestCase):
    def test_cron_harian_0830(self):
        p = parse("30 8 * * *")
        self.assertEqual(p["kind"], "cron")
        self.assertTrue(matches("30 8 * * *", epoch_lokal(2026, 6, 12, 8, 30)))
        self.assertFalse(matches("30 8 * * *", epoch_lokal(2026, 6, 12, 8, 31)))
        self.assertFalse(matches("30 8 * * *", epoch_lokal(2026, 6, 12, 9, 30)))

    def test_field_range(self):
        self.assertEqual(parse("0 9-17 * * *")["hour"], set(range(9, 18)))
        self.assertTrue(matches("0 9-17 * * *", epoch_lokal(2026, 6, 12, 9, 0)))
        self.assertTrue(matches("0 9-17 * * *", epoch_lokal(2026, 6, 12, 17, 0)))
        self.assertFalse(matches("0 9-17 * * *", epoch_lokal(2026, 6, 12, 8, 0)))
        self.assertFalse(matches("0 9-17 * * *", epoch_lokal(2026, 6, 12, 18, 0)))

    def test_field_list(self):
        p = parse("5,25 8 * * *")
        self.assertEqual(p["minute"], {5, 25})
        self.assertTrue(matches("5,25 8 * * *", epoch_lokal(2026, 6, 12, 8, 5)))
        self.assertTrue(matches("5,25 8 * * *", epoch_lokal(2026, 6, 12, 8, 25)))
        self.assertFalse(matches("5,25 8 * * *", epoch_lokal(2026, 6, 12, 8, 15)))

    def test_field_step_range(self):
        p = parse("0 8-12/2 * * *")
        self.assertEqual(p["hour"], {8, 10, 12})
        self.assertTrue(matches("0 8-12/2 * * *", epoch_lokal(2026, 6, 12, 10, 0)))
        self.assertFalse(matches("0 8-12/2 * * *", epoch_lokal(2026, 6, 12, 9, 0)))
        p2 = parse("*/15 0 * * *")          # stepped '*' without a range
        self.assertEqual(p2["minute"], set(range(0, 60, 15)))

    def test_semantik_dow_minggu_pertama(self):
        # both 7 and 0 mean Sunday (cron convention)
        a = parse("0 12 * * 7")
        b = parse("0 12 * * 0")
        self.assertEqual(a["dow"], b["dow"])
        self.assertIn(0, a["dow"])

    def test_semantik_dom_dow_OR(self):
        # both restricted → hit when either one matches (classic cron)
        e = epoch_lokal(2026, 6, 12, 12, 0)   # 2nd Friday
        self.assertTrue(matches("0 12 13 * 5", e))   # dow Friday


class TestInvalid(unittest.TestCase):
    def test_invalid_letters(self):
        with self.assertRaises(ValueError):
            parse("abc def * * *")

    def test_invalid_field_missing(self):
        with self.assertRaises(ValueError):
            parse("30 8 * *")
        with self.assertRaises(ValueError):
            parse("")

    def test_invalid_value_out_of_range(self):
        with self.assertRaises(ValueError):
            parse("0 25 * * *")          # hour 25
        with self.assertRaises(ValueError):
            parse("*/0")                 # division by zero
        with self.assertRaises(ValueError):
            parse("24:00")               # hour 24


class TestSempadanWaktu(unittest.TestCase):
    def test_feb31_tidak_pernah_cocok(self):
        # there is no February 31 → no occurrence within the horizon
        self.assertIsNone(next_run("0 5 31 2 *", EPOCH, horizon_days=370))
        # the parse is still structurally valid
        p = parse("0 5 31 2 *")
        self.assertEqual(p["mon"], {2})

    def test_matches_dengan_epoch_dt(self):
        self.assertTrue(matches("30 8 * * *", EPOCH))
        self.assertFalse(matches("30 8 * * *", EPOCH + 3600))   # epoch → 09:30
        self.assertFalse(matches("30 8 * * *", EPOCH - 60))
        self.assertTrue(matches("*/30 * * * *", EPOCH - 1800))  # floats are fine

    def test_matches_default_utcnow(self):
        # default dt = utcnow(): just make sure it does not blow up & the type is right
        self.assertIsInstance(matches("* * * * *"), bool)


class TestNextRun(unittest.TestCase):
    def test_next_run_setelah_2359_ke_esok_pagi(self):
        after = epoch_lokal(2026, 6, 12, 23, 59)
        hasil = next_run("08:00", after)
        self.assertEqual(hasil, epoch_lokal(2026, 6, 13, 8, 0))

    def test_next_run_interval_10_menit(self):
        after = epoch_lokal(2026, 6, 12, 7, 43)
        self.assertEqual(next_run("*/10", after), epoch_lokal(2026, 6, 12, 7, 50))
        # exactly on a multiple → the NEXT occurrence (strictly after)
        after2 = epoch_lokal(2026, 6, 12, 7, 50)
        self.assertEqual(next_run("*/10", after2), epoch_lokal(2026, 6, 12, 8, 0))

    def test_next_run_across_months_and_years(self):
        # Dec 31 → Jan 1: constant UTC+7 offset, no day drift
        after = epoch_lokal(2026, 12, 31, 23, 59)
        hasil = next_run("0 0 * * *", after)
        self.assertEqual(hasil, epoch_lokal(2027, 1, 1, 0, 0))
        u = datetime(2027, 1, 1, 0, 0) - timedelta(hours=7)
        self.assertEqual(calendar.timegm(u.utctimetuple()), hasil)
        self.assertTrue(matches("0 0 * * *", hasil))
        # around Feb: 28 Feb → 1 Mar
        after2 = epoch_lokal(2026, 2, 28, 23, 59)
        self.assertEqual(next_run("0 0 * * *", after2),
                         epoch_lokal(2026, 3, 1, 0, 0))

    def test_next_run_hormati_hari_minggu(self):
        # the next Sunday after Friday June 12 2026 = June 21 (Sunday)
        after = epoch_lokal(2026, 6, 12, 10, 0)
        self.assertEqual(next_run("30 6 * * 0", after),
                         epoch_lokal(2026, 6, 14, 6, 30))

    def test_next_run_exact_now_immediate(self):
        # after=None defaults to utcnow → the result epoch is > now
        import time
        hasil = next_run("* * * * *")
        self.assertIsNotNone(hasil)
        self.assertGreater(hasil, time.time() - 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
