#!/usr/bin/env python3
"""Tests for context_buffer.ChatBuffer — per-chat context ring buffer."""
import json
import os
import random
import tempfile
import threading
import unittest

from context_buffer import ChatBuffer


class ChatBufferTest(unittest.TestCase):

    def test_maxlen_membuang_paling_tua(self):
        b = ChatBuffer(maxlen=3)
        for i in range(5):
            b.append(1, "user", f"message-{i}")
        hasil = b.recent(1)
        self.assertEqual(len(hasil), 3)
        self.assertEqual([e["text"] for e in hasil], ["message-2", "message-3", "message-4"])

    def test_urutan_kronologis(self):
        b = ChatBuffer(maxlen=10)
        b.append(42, "user", "first", ts=1000.0)
        b.append(42, "bot", "second", ts=2000.0)
        b.append(42, "user", "third", ts=3000.0)
        hasil = b.recent(42)
        self.assertEqual([e["text"] for e in hasil], ["first", "second", "third"])
        self.assertEqual([e["role"] for e in hasil], ["user", "bot", "user"])
        # recent(n) → the last n, still ordered oldest→newest
        potong = b.recent(42, 2)
        self.assertEqual([e["text"] for e in potong], ["second", "third"])

    def test_role_invalid_ditolak(self):
        b = ChatBuffer(maxlen=5)
        self.assertFalse(b.append(1, "admin", "hello"))
        self.assertFalse(b.append(1, "", "hello"))
        self.assertEqual(b.recent(1), [])

    def test_chats_are_separate(self):
        b = ChatBuffer(maxlen=5)
        b.append("a", "user", "in A")
        b.append("b", "user", "in B")
        self.assertEqual([e["text"] for e in b.recent("a")], ["in A"])
        self.assertEqual([e["text"] for e in b.recent(987)], [])  # empty chat → []

    def test_format_context(self):
        b = ChatBuffer(maxlen=10)
        b.append(7, "user", "morning world", name="Sinta", ts="2026-10-30 07:05:00")
        b.append(7, "bot", "good morning!", name="Rio", ts="2026-10-30T07:46:00")
        teks = b.format_context(7)
        self.assertIn("[07:05] Sinta: morning world", teks)
        self.assertIn("[07:46] Rio: good morning!", teks)
        self.assertEqual(len(teks.splitlines()), 2)

    def test_format_context_truncates_long(self):
        b = ChatBuffer(maxlen=10)
        panjang = "x" * 300
        b.append(9, "user", panjang, name="Beta", ts=1000.0)
        baris = b.format_context(9)
        # text longer than 120 chars is clipped (with the marker), a single line only
        self.assertEqual(len(baris.splitlines()), 1)
        self.assertIn("Beta", baris)
        self.assertIn("…", baris)
        self.assertNotIn("x" * 121, baris)  # definitely not intact
        self.assertLessEqual(len(baris), 140)  # prefix "[HH:MM] name: " + 121
        self.assertGreaterEqual(baris.count("x"), 120)

    def test_format_context_empty_returns_string(self):
        b = ChatBuffer(maxlen=10)
        self.assertEqual(b.format_context(404), "")
        # a chat that exists but whose buffer was emptied via a small maxlen is safe too
        b.append(405, "user", "one")
        self.assertNotEqual(b.format_context(405), "")

    def test_save_load_roundtrip(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            b1 = ChatBuffer(maxlen=40, snapshot_path=path)
            b1.append(11, "user", "old hello", name="Ayu", ts=1700000100.0)
            b1.append(11, "bot", "hello back", name="Rio", ts=1700000160.0)
            b1.append(22, "user", "another chat", name="Cika", ts=1700000200.0)
            self.assertTrue(b1.save())

            b2 = ChatBuffer(maxlen=40, snapshot_path=path)
            self.assertTrue(b2.load())
            self.assertEqual([e["text"] for e in b2.recent(11)],
                             ["old hello", "hello back"])
            self.assertEqual(b2.recent(11)[0]["name"], "Ayu")
            self.assertEqual([e["text"] for e in b2.recent(22)], ["another chat"])
            # the format is still correct after load
            self.assertIn("Rio", b2.format_context(11))
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_load_corrupt_without_exception(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w") as f:
                f.write("{this is not json,,,[")
            b = ChatBuffer(maxlen=5, snapshot_path=path)
            self.assertFalse(b.load())  # does not raise
            self.assertEqual(b.recent(1), [])
            # valid JSON with a wrong shape is tolerated as well
            with open(path, "w") as f:
                json.dump(["not", "a-dict"], f)
            self.assertFalse(b.load())
            with open(path, "w") as f:
                json.dump({"1": "not-a-list"}, f)
            b.load()  # anomalous entries dropped without raising
            self.assertEqual(b.recent(1), [])
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_thread_safety_smoke(self):
        b = ChatBuffer(maxlen=40)
        kunci_chat = "stress"

        def pengisi(seed):
            rng = random.Random(seed)
            for i in range(60):
                b.append(kunci_chat, rng.choice(["user", "bot"]),
                         f"t{seed}-{i}", name=f"n{seed}", ts=random.random() * 1e9)

        threads = [threading.Thread(target=pengisi, args=(s,)) for s in range(8)]
        for t in threads:
            t.start()
        para_peredang = []
        for _ in range(4):
            def baca():
                for _ in range(60):
                    hasil = b.recent(kunci_chat)
                    self.assertLessEqual(len(hasil), 40)
                    for e in hasil:
                        self.assertIn(e["role"], ("user", "bot"))
                    b.format_context(kunci_chat)
            th = threading.Thread(target=baca)
            para_peredang.append(th)
        for t in para_peredang:
            t.start()
        for t in threads + para_peredang:
            t.join()
        hasil = b.recent(kunci_chat)
        self.assertLessEqual(len(hasil), 40)
        self.assertGreater(len(hasil), 0)
        for e in hasil:
            self.assertIn(e["role"], ("user", "bot"))
            self.assertTrue(e["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
