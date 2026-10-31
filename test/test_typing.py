"""Typing indicator: the bot shows 'typing…' while it works, best-effort."""
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
    def test_default_chat_has_no_extra_ack_roundtrip(self):
        g = make_gateway()
        g.config = {}
        g.presence = Mock()
        g.authorize.return_value = True
        g.jobs.accept.return_value = (14, True)
        g.accept({'token': '999:t'}, {'update_id': 1, 'message': {
            'from': {'id': 2}, 'chat': {'id': 777, 'type': 'private'}, 'text': 'hi'}})
        g.jobs.acknowledge.assert_called_once_with(14)
        g.presence.acknowledge.assert_not_called()
        g.call.assert_not_called()

    def setUp(self):
        self.gateway = make_gateway()

    def test_execute_requests_typing_without_waiting_for_network(self):
        g = make_gateway()
        bot = {"token": "t", "username": "Una", "bot_id": "999"}
        g.authorize = lambda user: True
        g.media.receive = Mock(return_value=("", ""))
        g.schedules.interpret = Mock(return_value=None)
        g.conversations.answer = Mock()
        g.presence = Mock()
        g.runtime.current_job.return_value = {'id': 1}
        g.execute(bot, {"message": {"message_id": 1, "from": {"id": 2, "first_name": "O"},
                    "chat": {"id": 777, "type": "private"}, "text": "hi"},
                    "update_id": 9}, "Una")
        g.presence.typing.assert_called_once_with(1)
        g.conversations.answer.assert_called_once()
        g.call.assert_not_called()

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

    def test_cancel_button_is_a_control_even_when_chat_is_fenced(self):
        g = make_gateway()
        g.authorize.return_value = True
        g.jobs.accept.return_value = (None, True)
        # Suppress the cosmetic thread; the important fence is enqueue=False.
        from unittest.mock import patch
        with patch('openbridge.adapters.channels.telegram.gateway.threading.Thread'):
            g.accept({'token': '123:t'}, {'update_id': 1, 'callback_query': {
                'from': {'id': 42}, 'data': 'jx-99',
                'message': {'chat': {'id': 42, 'type': 'private'}}}})
        self.assertFalse(g.jobs.accept.call_args.kwargs['enqueue'])


if __name__ == "__main__":
    unittest.main()
