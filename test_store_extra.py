#!/usr/bin/env python3
"""Tes tambahan store.py: tabel chats & personas."""
import os
import shutil
import tempfile
import unittest

from store import Store


class TestChatsPersonas(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        self.s = Store(self.db)

    def tearDown(self):
        self.s.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_save_chat_upsert(self):
        self.s.save_chat(123, "Geng Belajar")
        self.s.save_chat(123, "Geng Belajar 2")
        self.s.save_chat("abc", "DM Kirman")
        semua = self.s.list_chats()
        self.assertEqual(len(semua), 2)
        b_filter = [c for c in semua if c["chat_id"] == "123"]
        self.assertEqual(b_filter[0]["judul"], "Geng Belajar 2")  # upsert menimpa
        self.assertIn("updated_at", b_filter[0])

    def test_list_chats_urut_baru(self):
        self.s.save_chat(1, "A")
        self.s.save_chat(2, "B")
        self.assertEqual(self.s.list_chats()[0]["chat_id"], "2")

    def test_persona_upsert(self):
        self.s.upsert_persona("Unabot", "sobat grup")
        self.s.upsert_persona("Unabot", "sobat grup v2")  # upsert, tidak dobel
        ps = self.s.list_personas()
        self.assertEqual(len(ps), 1)
        self.assertEqual(ps[0]["persona"], "sobat grup v2")

    def test_persona_baris_lengkap(self):
        self.s.upsert_persona("Senja", "tenang")
        p = self.s.list_personas()[0]
        self.assertIn("created_at", p)
        self.assertIn("updated_at", p)

    def test_persona_validasi(self):
        with self.assertRaises(ValueError):
            self.s.upsert_persona("", "halo")
        with self.assertRaises(ValueError):
            self.s.upsert_persona("X", "")

    def test_hapus_persona_idempoten(self):
        self.s.upsert_persona("Gonja", "kocak")
        self.assertTrue(self.s.hapus_persona("Gonja"))
        self.assertFalse(self.s.hapus_persona("Gonja"))  # sudah tidak ada
        self.assertEqual(self.s.list_personas(), [])

    def test_tabel_lama_tak_rusak(self):
        # ketika menjalankan Store yang sudah ada di db produk: jadwal aman
        self.s.add_schedule("900", 555, "21:00", "cron", "sapa")
        self.s.save_chat(555, "Geng")
        jadwal = self.s.list_schedules(active_only=True)
        self.assertEqual(jadwal[0]["chat_id"], 555)


if __name__ == "__main__":
    unittest.main()
