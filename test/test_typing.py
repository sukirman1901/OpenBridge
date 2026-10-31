"""Typing indicator: the bot shows 'typing…' while it works, best-effort."""
import threading
import unittest
from unittest.mock import Mock

from openbridge.adapters.channels.telegram.gateway import TelegramGateway


def make_gateway():
    gateway = TelegramGateway(config=Mock(), call=Mock(), jobs=Mock(), store=Mock(),
        runtime=Mock(), context=Mock(), conversations=Mock(), commands=Mock(),
        media=Mock(), schedules=Mock(), authorize=Mock(), send=Mock(),
        controls=Mock(), stopped=Mock(), log=Mock())
    gateway.jobs.is_blocked.return_value = False
    return gateway


class TypingLoop(unittest.TestCase):
    def setUp(self):
        self.gateway = make_gateway()

    def test_typing_calls_send_chataction_until_released(self):
        bot, chat_id = {"token": "tok"}, 777
        calls = []
        called = threading.Event()
        def call(token, method, payload, timeout=45):
            calls.append(method)
            called.set()
            return {}
        self.gateway.call = call
        done = threading.Event()
        worker = threading.Thread(target=self.gateway.typing_action, args=(bot, chat_id, done))
        worker.start()
        self.assertTrue(called.wait(2))
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

    def test_acknowledgement_has_no_internal_job_number(self):
        g = make_gateway()
        g.authorize.return_value = True
        g.jobs.accept.return_value = (14, True)
        g.call.return_value = {"ok": True, "result": {"message_id": 99}}
        g.accept({"token": "999:t"}, {"update_id": 1, "message": {
            "from": {"id": 2}, "chat": {"id": 777, "type": "private"}, "text": "hi"}})
        self.assertEqual(g.call.call_args.args[2]["text"], "⏳ Queued…")
        g.jobs.acknowledge.assert_called_once_with(14, 99)

    def test_blocked_conversation_reports_review_not_processing(self):
        g = make_gateway()
        g.authorize.return_value = True
        g.jobs.accept.return_value = (14, True)
        g.jobs.is_blocked.return_value = True
        g.call.return_value = {"ok": True, "result": {"message_id": 99}}
        g.accept({"token": "999:t"}, {"update_id": 1, "message": {
            "from": {"id": 2}, "chat": {"id": 777, "type": "private"}, "text": "hi"}})
        notice = g.call.call_args.args[2]['text']
        self.assertIn('paused', notice)
        self.assertIn('/jobs', notice)
        self.assertNotIn('Processing', notice)
        g.jobs.acknowledge.assert_called_once_with(14, 99)

    def test_unaddressed_group_message_does_not_show_typing(self):
        g = make_gateway()
        g.runtime.current_job.return_value = None
        g.authorize.return_value = True
        g.media.receive.return_value = ("", "")
        g.schedules.interpret.return_value = None
        g.execute({"token": "999:t"}, {"message": {
            "message_id": 3, "from": {"id": 2}, "chat": {"id": 777, "type": "group"},
            "text": "talking to someone else"}}, "Una")
        g.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
