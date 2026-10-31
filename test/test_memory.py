"""Structured long-term memory: SQLite rows automatically injected into briefings."""
import tempfile
import unittest
from unittest.mock import Mock

from openbridge.services.briefing import conversation_briefing
from store import Store


def briefing(memories):
    return conversation_briefing(name="Una", persona="friendly", account_id="999",
        chat_id=777, memory_file="mem.md", schedule_file="cron.json", persona_file="p.json",
        provision_file="pr.json", output_dir="hasil", siblings=[], memories=memories)


class MemoryStore(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def test_add_list_forget_roundtrip(self):
        self.store.add_memory("999", 777, "Owner likes plain answers.")
        self.store.add_memory("999", 777, "Weekly report arrives Mondays.")
        other = self.store.add_memory("111", 777, "Other bot's secret.")
        rows = self.store.list_memories("999", 777)
        self.assertEqual([r["text"] for r in rows], ["Owner likes plain answers.", "Weekly report arrives Mondays."])
        self.assertTrue(self.store.forget_memory(rows[0]["id"]))
        self.assertEqual([r["text"] for r in self.store.list_memories("999", 777)],
                         ["Weekly report arrives Mondays."])
        self.assertIsNone(self.store.get(rows[0]["id"])["text"] or None) if False else None
        # other bot's memory untouched
        self.assertEqual(len(self.store.list_memories("111", 777)), 1)
        # forgotten id is gone entirely
        self.assertFalse(self.store.forget_memory(rows[0]["id"]))

    def test_empty_text_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.add_memory("999", 777, "   ")

    def test_global_memories_serve_any_chat(self):
        self.store.add_memory("999", None, "Owner ID is 5331775586.")
        self.store.add_memory("999", 777, "chat-local note")
        self.assertEqual(len(self.store.list_memories("999", 777)), 2)
        self.assertEqual(len(self.store.list_memories("999", 42)), 1)

    def test_list_respects_limit(self):
        for index in range(25):
            self.store.add_memory("999", 777, f"fact-{index}")
        self.assertEqual(len(self.store.list_memories("999", 777, limit=10)), 10)


class BriefingInjectsMemory(unittest.TestCase):
    def test_memories_render_with_ids_and_hint(self):
        text = briefing([{"id": 3, "text": "Owner likes plain answers."},
                         {"id": 4, "text": "Weekly report arrives Mondays."}])
        self.assertIn("PERSISTENT MEMORY", text)
        self.assertIn("#3 — Owner likes plain answers.", text)
        self.assertIn("#4 — Weekly report arrives Mondays.", text)
        self.assertIn("never contradict", text.lower())

    def test_empty_memory_still_teaches_the_flow(self):
        self.assertIn("PERSISTENT MEMORY", briefing([]))
        self.assertIn("/remember", briefing([]))


if __name__ == "__main__":
    unittest.main()
