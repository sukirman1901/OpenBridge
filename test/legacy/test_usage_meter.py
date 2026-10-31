# unittest untuk usage_meter.py — meteran budget harian
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import usage_meter
from usage_meter import UsageMeter


def hari(tanggal):
    """Patch jam lokal: paksa meteran melihat 'tanggal' tertentu."""
    return patch.object(usage_meter, "local_day", lambda ts=None: tanggal)


def sampel_sesi():
    """Bentuk yang sudah diamati dari OpenCode."""
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
        with hari("2026-01-01"):
            m.record("botA", calls=1, cost=0.10, tokens=10)
        with hari("2026-01-02"):
            # hari baru: mulai dari nol (mekaris sendiri)
            self.assertEqual(m.today("botA"), {"calls": 0, "cost": 0.0, "tokens": 0})
            m.record("botA", calls=1, cost=0.20, tokens=20)
            self.assertEqual(m.today("botA"), {"calls": 1, "cost": 0.20, "tokens": 20})
        # hari lama tetap tersimpan di struktur
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
        # dict kosong, None, sampah, dan message punya 'model' + pembungkus data
        m.extract_and_record("botA", {})
        m.extract_and_record("botB", None)
        m.extract_and_record("botC", "bukan-dict")
        m.extract_and_record("botA", {"model": "x/y", "tokens": "rusak"})
        m.extract_and_record("botA", {"data": {"cost": 0.02,
                                               "tokens": {"input": 10}}})
        self.assertEqual(m.today("botA")["calls"], 3)
        self.assertEqual(m.today("botA")["cost"], 0.02)
        self.assertEqual(m.today("botB")["cost"], 0.0)
        self.assertEqual(m.today("botC")["tokens"], 0)

    def test_5_over_budget_tepat_di_batas(self):
        m = UsageMeter(daily_call_cap=5, daily_cost_cap=1.0)
        m.record("botA", calls=4, cost=0.80)
        # kandidat yang pas mengisi sisa → masih boleh
        self.assertFalse(m.over_budget("botA", calls=1, cost=0.20))
        # satu lagi melewati batas → tertolak (calls & cost)
        self.assertTrue(m.over_budget("botA", calls=2))
        self.assertTrue(m.over_budget("botA", cost=0.21))
        m.record("botA", calls=1, cost=0.20)  # sekarang persis di batas
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
        # meteran baru dari file yang sama melihat data yang sama
        m2 = UsageMeter(path=self.path)
        self.assertEqual(m2.today("botA"),
                         {"calls": 2, "cost": 0.15, "tokens": 75})
        self.assertEqual(m1.today("botB"), m2.today("botB"))

    def test_8_file_rusak_mulai_bersih(self):
        with open(self.path, "w") as f:
            f.write("{ini bukan json!!!")
        m = UsageMeter(path=self.path)  # tidak boleh exception
        self.assertEqual(m.today("botA"), {"calls": 0, "cost": 0.0, "tokens": 0})
        m.record("botA", calls=1, cost=0.01, tokens=3)
        # data baru harus tetap bisa ditulis & dibaca ulang
        m2 = UsageMeter(path=self.path)
        self.assertEqual(m2.today("botA")["calls"], 1)


if __name__ == "__main__":
    unittest.main()
