"""Saved Telegram ingress through explicitly assembled application components."""
import unittest
from unittest.mock import Mock

from test.support import make_application
from openbridge.services.conversations import session_key
from openbridge.domain.messages import ConversationKey
from openbridge.domain.errors import ExecutionUncertain


class TestBridgeDispatch(unittest.TestCase):
    def test_polled_messages_are_sequential_and_keep_their_own_context(self):
        updates = [{"update_id": index, "message": {"message_id": index, "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42, "first_name": "Owner"}, "text": text}}
                   for index, text in enumerate(("first", "second"), 1)]
        batches, sent, prompts = [updates], [], []
        def call(token, method, payload=None, timeout=45):
            if method == "getMe":
                return {"ok": True, "result": {"id": 123, "username": "testbot"}}
            if method == "getUpdates":
                if batches:
                    return {"ok": True, "result": batches.pop()}
                app.shutdown.set()
                return {"ok": True, "result": []}
            if method == "sendMessage" and payload["text"].startswith("answer-"):
                sent.append(payload["text"])
            return {"ok": True}
        app = make_application(self, call=call)
        components = app.components
        components.conversations.get = Mock(return_value="session-test")
        def prompt(sid, text, **options):
            prompts.append(text)
            return f"answer-{len(prompts)}"
        components.backend.run_prompt.side_effect = prompt
        components.gateway.poll(components.configuration.data["bots"][0])
        self.assertEqual(prompts, [])
        self.assertEqual(components.jobs.cursor("telegram", "123"), 3)
        components.runtime.run(1)
        components.runtime.run(2)
        while components.runtime.deliver_one():
            pass
        self.assertEqual(len(prompts), 2)
        self.assertIn("⟦UNTRUSTED MESSAGE FROM Owner⟧\nfirst", prompts[0])
        self.assertNotIn("second", prompts[0])
        self.assertIn("answer-1", prompts[1])
        self.assertIn("⟦UNTRUSTED MESSAGE FROM Owner⟧\nsecond", prompts[1])
        self.assertEqual(sent, ["answer-1", "answer-2"])

    def test_session_identity_includes_account_and_execution_mode(self):
        persona = {"nama": "Una"}
        self.assertNotEqual(session_key({"token": "123:a"}, 42, persona), session_key({"token": "456:b"}, 42, persona))
        self.assertNotEqual(session_key({"token": "123:a"}, 42, persona), session_key({"token": "123:a"}, 42, persona, "tugas"))

    def test_inbox_commits_before_ack_and_duplicate_has_no_second_ack(self):
        def call(*args, **options):
            self.assertEqual(app.components.jobs.cursor("telegram", "123"), 6)
            self.assertEqual(app.components.jobs.get(1)["status"], "queued")
            return {"ok": True, "result": {"message_id": 99}}
        sender = Mock(side_effect=call)
        app = make_application(self, call=sender)
        bot = app.components.configuration.data["bots"][0]
        update = {"update_id": 5, "message": {"message_id": 5, "text": "hello", "from": {"id": 42}, "chat": {"id": 42, "type": "private"}}}
        app.components.gateway.accept(bot, update)
        app.components.gateway.accept(bot, update)
        sender.assert_called_once()
        self.assertEqual(app.components.jobs.get(1)["status_message_id"], 99)
        self.assertNotIn(bot["token"], app.components.jobs.get(1)["payload"])

    def test_untrusted_updates_cannot_create_jobs_or_download_attachments(self):
        sender = Mock()
        app = make_application(self, call=sender)
        components = app.components
        components.gateway.media.receive = Mock()
        update = {"update_id": 5, "message": {"message_id": 5, "text": "hello", "from": {"id": 999}, "chat": {"id": 42, "type": "private"}}}
        components.gateway.accept(components.configuration.data["bots"][0], update)
        self.assertEqual(components.jobs.pending(), [])
        self.assertEqual(components.jobs.cursor("telegram", "123"), 6)
        self.assertEqual(components.jobs.db.execute("SELECT payload FROM runtime_inbox").fetchone()[0], "{}")
        sender.assert_not_called()
        components.gateway.media.receive.assert_not_called()

    def test_reconciliation_fails_closed_when_backend_is_active(self):
        app = make_application(self)
        components = app.components
        identity, _ = components.jobs.accept(ConversationKey("telegram", "123", "42"), 1, {})
        components.jobs.acknowledge(identity)
        components.jobs.claim(identity)
        components.jobs.checkpoint(identity, "session")
        components.jobs.finish(identity, [], "interrupted")
        components.backend.session_idle.return_value = False
        components.gateway.controls(components.configuration.data["bots"][0], 42, f"/resolve {identity} confirm")
        self.assertEqual(components.jobs.get(identity)["status"], "interrupted")

    def test_delivery_retry_and_parse_fallback_do_not_call_execution(self):
        sender = Mock(side_effect=[{"error": 400, "body": "can't parse entities"}, {"error": 400, "body": "message is not modified"}])
        app = make_application(self, call=sender)
        delivery = {"job_id": 1, "id": 1, "account_id": "123", "conversation_id": "42",
                    "payload": {"operation": "edit", "message_id": 99, "text": "answer", "parse": True}}
        self.assertEqual(app.components.delivery.deliver(delivery), ("sent", None, None))
        self.assertEqual(sender.call_count, 2)
        app.components.backend.run_prompt.assert_not_called()

    def test_ambiguous_send_is_not_automatically_replayed(self):
        app = make_application(self, call=Mock(return_value={"error": "URLError"}))
        delivery = {"job_id": 1, "id": 1, "account_id": "123", "conversation_id": "42", "payload": {"operation": "send", "text": "answer"}}
        self.assertEqual(app.components.delivery.deliver(delivery)[0], "uncertain")

    def test_job_alias_lists_jobs_and_shows_scoped_running_job(self):
        sender = Mock(return_value={"ok": True})
        app = make_application(self, call=sender)
        components = app.components
        identity, _ = components.jobs.accept(ConversationKey("telegram", "123", "42"), 1, {})
        components.jobs.acknowledge(identity)
        components.jobs.claim(identity)
        components.jobs.checkpoint(identity, "session")
        components.gateway.controls(components.configuration.data["bots"][0], 42, "/job")
        self.assertIn("running", sender.call_args.args[2]["text"])
        components.gateway.controls(components.configuration.data["bots"][0], 42, f"/job {identity}")
        buttons = sender.call_args.args[2]["reply_markup"]["inline_keyboard"]
        self.assertEqual(buttons[0][0]["callback_data"], "st-session")

    def test_job_alias_is_recognized_before_execution_queue(self):
        app = make_application(self)
        components = app.components
        components.gateway.controls = Mock()
        bot = components.configuration.data["bots"][0]
        update = {"update_id": 10, "message": {"message_id": 10, "text": "/job", "from": {"id": 42}, "chat": {"id": 42, "type": "private"}}}
        components.gateway.accept(bot, update)
        components.gateway.controls.assert_called_once_with(bot, 42, "/job")
        self.assertEqual(components.jobs.pending(), [])

    def test_uncertain_response_cannot_deliver_partial_text(self):
        app = make_application(self)
        components = app.components
        components.conversations.get = Mock(return_value="session")
        components.backend.run_prompt.side_effect = ExecutionUncertain("partial result is not final")
        bot = components.configuration.data["bots"][0]
        update = {"update_id": 1, "message": {"message_id": 1, "text": "hello", "from": {"id": 42}, "chat": {"id": 42, "type": "private"}}}
        components.gateway.accept(bot, update)
        components.runtime.run(1)
        self.assertEqual(components.jobs.get(1)["status"], "interrupted")
        self.assertNotIn("partial result", components.jobs.get(1)["result"])
