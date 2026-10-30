# unittest for usage_meter.py — daily budget meter
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import usage_meter
from usage_meter import UsageMeter


def day(tanggal):
    """Patch the local clock: force the meter to see a given 'date'."""
    return patch.object(usage_meter, "local_day", lambda ts=None: tanggal)


def sampel_sesi():
    """The shape actually observed from OpenCode."""
    return {
        "id": "ses-1",
        "cost": 0.05,
        "tokens": {"input": 100, "output": 50, "reasoning": 10,
                   "cache": {"read": 40, "write": 5}},
    }


class TestUsageMeter(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="meter-")
        self.path = os.path.join(self.dir, "usage.json")

    def test_1_record_akumulasi(self):
        m = UsageMeter()
        m.record("botA", calls=1, cost=0.01, tokens=100)
        m.record("botA", calls=2, cost=0.02, tokens=50)
        m.record("botB", calls=1, cost=0.10, tokens=7)
        self.assertEqual(m.today("botA"),
                         {"calls": 3, "cost": 0.03, "tokens": 150})
        self.assertEqual(m.today("botB")["calls"], 1)
        self.assertEqual(m.today("botB")["cost"], 0.10)

    def test_2_pisah_hari_beda(self):
        m = UsageMeter()
        with day("2026-01-01"):
            m.record("botA", calls=1, cost=0.10, tokens=10)
        with day("2026-01-02"):
            # new day: starts from zero (metrologically clean)
            self.assertEqual(m.today("botA"), {"calls": 0, "cost": 0.0, "tokens": 0})
            m.record("botA", calls=1, cost=0.20, tokens=20)
            self.assertEqual(m.today("botA"), {"calls": 1, "cost": 0.20, "tokens": 20})
        # the old day remains stored in the structure
        self.assertEqual(m._data["2026-01-01"]["botA"]["cost"], 0.10)
        self.assertEqual(m._data["2026-01-02"]["botA"]["tokens"], 20)

    def test_3_extract_dict_lengkap(self):
        m = UsageMeter()
        cost, tokens = m.extract_and_record("botA", sampel_sesi())
        self.assertEqual(cost, 0.05)
        self.assertEqual(tokens, 100 + 50 + 10 + 40 + 5)
        self.assertEqual(m.today("botA"), {"calls": 1, "cost": 0.05, "tokens": 205})

    def test_4_extract_field_hilang_dan_bentuk_aneh(self):
        m = UsageMeter()
        # empty dict, None, junk, and a message with 'model' + a data wrapper
        m.extract_and_record("botA", {})
        m.extract_and_record("botB", None)
        m.extract_and_record("botC", "not-a-dict")
        m.extract_and_record("botA", {"model": "x/y", "tokens": "broken"})
        m.extract_and_record("botA", {"data": {"cost": 0.02,
                                               "tokens": {"input": 10}}})
        self.assertEqual(m.today("botA")["calls"], 3)
        self.assertEqual(m.today("botA")["cost"], 0.02)
        self.assertEqual(m.today("botB")["cost"], 0.0)
        self.assertEqual(m.today("botC")["tokens"], 0)

    def test_5_over_budget_tepat_di_batas(self):
        m = UsageMeter(daily_call_cap=5, daily_cost_cap=1.0)
        m.record("botA", calls=4, cost=0.80)
        # a candidate that exactly fills the remainder → still allowed
        self.assertFalse(m.over_budget("botA", calls=1, cost=0.20))
        # one more would cross the limit → rejected (calls & cost)
        self.assertTrue(m.over_budget("botA", calls=2))
        self.assertTrue(m.over_budget("botA", cost=0.21))
        m.record("botA", calls=1, cost=0.20)  # now exactly at the limit
        self.assertFalse(m.over_budget("botA"))
        self.assertTrue(m.over_budget("botA", calls=1, cost=0.001))

    def test_6_cap_none_tak_berbatas(self):
        m = UsageMeter()  # None = ∞
        m.record("botA", calls=9999, cost=9999.0)
        self.assertFalse(m.over_budget("botA", calls=9999, cost=9999.0))

    def test_7_persist_roundtrip(self):
        m1 = UsageMeter(path=self.path)
        m1.record("botA", calls=2, cost=0.15, tokens=75)
        m1.record("botB", calls=1, cost=0.05, tokens=5)
        # a new meter from the same file sees the same data
        m2 = UsageMeter(path=self.path)
        self.assertEqual(m2.today("botA"),
                         {"calls": 2, "cost": 0.15, "tokens": 75})
        self.assertEqual(m1.today("botB"), m2.today("botB"))

    def test_8_file_rusak_mulai_bersih(self):
        with open(self.path, "w") as f:
            f.write("{this is not json!!!")
        m = UsageMeter(path=self.path)  # must not raise
        self.assertEqual(m.today("botA"), {"calls": 0, "cost": 0.0, "tokens": 0})
        m.record("botA", calls=1, cost=0.01, tokens=3)
        # new data must still be writeable & readable again
        m2 = UsageMeter(path=self.path)
        self.assertEqual(m2.today("botA")["calls"], 1)


if __name__ == "__main__":
    unittest.main()
