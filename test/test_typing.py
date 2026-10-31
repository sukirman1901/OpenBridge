"""Typing indicator: the bot shows 'typing…' while it works, best-effort."""
import threading
import unittest
from unittest.mock import Mock

from openbridge.adapters.channels.telegram.gateway import TelegramGateway


def make_gateway():
    return TelegramGateway(config=Mock(), call=Mock(), jobs=Mock(), store=Mock(),
        runtime=Mock(), context=Mock(), conversations=Mock(), commands=Mock(),
        media=Mock(), schedules=Mock(), authorize=Mock(), send=Mock(),
        controls=Mock(), stopped=Mock(), log=Mock())


class TypingLoop(unittest.TestCase):
    def setUp(self):
        self.gateway = make_gateway()

    def test_typing_calls_send_chataction_until_released(self):
        bot, chat_id = {"token": "tok"}, 777
        calls = []
        self.gateway.call = lambda token, method, payload, timeout=45: (
            calls.append(method) or {})
        done = threading.Event()
        worker = threading.Thread(target=self.gateway.typing_action, args=(bot, chat_id, done))
        worker.start()
        self.assertNotIn(worker, [])  # wiring sanity
        done.set()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertTrue(calls)
        self.assertEqual(calls[0], "sendChatAction")

    def test_typing_survives_a_failing_channel_and_stops(self):
        self.gateway.call = Mock(side_effect=RuntimeError("network"))
        done = threading.Event()
        worker = threading.Thread(target=self.gateway.typing_action,
                                  args=({"token": "t"}, 1, done))
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())

    def test_execute_shows_typing_while_answering(self):
        g = make_gateway()
        bot = {"token": "t", "username": "Una", "bot_id": "999"}
        g.authorize = lambda user: True
        g.media.receive = Mock(return_value=("", ""))
        g.schedules.interpret = Mock(return_value=None)
        g.conversations.answer = Mock()
        order = []
        g.call = lambda token, method, payload=None, timeout=45: (order.append(method) or {})
        g.conversations.answer = lambda bot, chat_id, sender, text, kind: order.append("answer")
        g.execute(bot, {"message": {"message_id": 1, "from": {"id": 2, "first_name": "O"},
                    "chat": {"id": 777, "type": "private"}, "text": "hi"},
                    "update_id": 9}, "Una")
        self.assertIn("sendChatAction", order)
        self.assertLess(order.index("sendChatAction"), order.index("answer"))


if __name__ == "__main__":
    unittest.main()
