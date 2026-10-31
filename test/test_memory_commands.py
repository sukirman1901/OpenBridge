"""Owner memory commands: /remember /memory /forget, scoped per bot and chat."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from openbridge.adapters.channels.telegram.commands import TelegramCommands
from store import Store

BOT = {"token": "t", "bot_id": "999", "nama": "Una"}
KEY = dict(config=Mock(), backend=Mock(), conversations=Mock(), personas=Mock(),
           schedules=Mock(), tasks=Mock(), jobs=Mock(), approvals=Mock(),
           provision=Mock(), authorize=lambda user: True, edit=Mock(), call=Mock(), log=Mock())


def make_commands(test):
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    store = Store(str(Path(temporary.name) / 'mem.db'))
    test.addCleanup(store.close)
    return TelegramCommands(store=store,
                            send=Mock(), **KEY)


class MemoryCommands(unittest.TestCase):
    def setUp(self):
        self.commands = make_commands(self)
        self.send = self.commands.send

    def test_remember_stores_a_chat_fact(self):
        self.commands.handle(BOT, 777, "remember", "Owner likes plain answers")
        rows = self.commands.store.list_memories("999", 777)
        self.assertEqual(rows[0]["text"], "Owner likes plain answers")
        self.assertIn("noted", self.send.call_args[0][2].lower())

    def test_remember_needs_text(self):
        self.commands.handle(BOT, 777, "remember", "")
        self.assertEqual(self.commands.store.list_memories("999", 777), [])
        self.assertIn("what should i remember", self.send.call_args[0][2].lower())

    def test_memory_lists_ids(self):
        self.commands.handle(BOT, 777, "remember", "fact one")
        self.commands.handle(BOT, 777, "remember", "fact two")
        self.send.call_args_list.clear()
        self.commands.handle(BOT, 777, "memory")
        text = self.send.call_args[0][2]
        self.assertIn("#1", text) and self.assertIn("#2", text)
        self.assertIn("fact one", text) and self.assertIn("fact two", text)

    def test_memory_empty_state_teaches_usage(self):
        self.commands.handle(BOT, 777, "memory")
        self.assertIn("/remember", self.send.call_args[0][2])

    def test_forget_removes_by_id(self):
        self.commands.handle(BOT, 777, "remember", "obsolete fact")
        self.commands.handle(BOT, 777, "forget", "1")
        self.assertEqual(self.commands.store.list_memories("999", 777), [])
        self.assertIn("forgotten", self.send.call_args[0][2].lower())

    def test_forget_rejects_unknown_or_foreign_ids(self):
        self.commands.handle(BOT, 777, "forget", "42")
        self.assertIn("not found", self.send.call_args[0][2].lower())
        self.commands.handle(BOT, 777, "forget", "abc")
        self.assertIn("usage", self.send.call_args[0][2].lower())

    def test_forget_is_bot_scoped(self):
        identity = self.commands.store.add_memory("111", 777, "other bot's fact")
        self.commands.handle(BOT, 777, "forget", str(identity))
        self.assertEqual(len(self.commands.store.list_memories("111", 777)), 1)


if __name__ == "__main__":
    unittest.main()
