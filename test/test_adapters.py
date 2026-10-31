import io
import json
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

from openbridge.adapters.http import JsonHttpClient
from openbridge.adapters.channels.telegram import TelegramAdapter, TelegramClient
from openbridge.adapters.execution.opencode import OpenCodeClient
from openbridge.domain.messages import ConversationKey


class TestHttpTransport(unittest.TestCase):
    def test_json_auth_and_timeout(self):
        opener = Mock(return_value=io.BytesIO(b'{"ok":true}'))
        client = JsonHttpClient(opener=opener)
        self.assertEqual(client("http://local/api", "POST", {"text": "hello"},
                                ("name", "password"), timeout=7), {"ok": True})
        request = opener.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {"text": "hello"})
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))
        self.assertEqual(opener.call_args.kwargs["timeout"], 7)

    def test_get_retries_but_post_is_not_replayed(self):
        for method, expected in (("GET", 3), ("POST", 1), ("PATCH", 1), ("DELETE", 1)):
            with self.subTest(method=method):
                opener = Mock(side_effect=URLError("https://secret-token/private"))
                client = JsonHttpClient(opener, sleep=Mock())
                result = client("http://local", method)
                self.assertEqual(opener.call_count, expected)
                self.assertEqual(result, {"error": "URLError"})
                self.assertNotIn("secret-token", str(result))

    def test_http_errors_keep_status_and_do_not_retry(self):
        opener = Mock(side_effect=HTTPError("http://local", 429, "limited", {},
                                           io.BytesIO(b"try later")))
        self.assertEqual(JsonHttpClient(opener)("http://local"),
                         {"error": 429, "body": "try later"})
        self.assertEqual(opener.call_count, 1)

    def test_empty_success_body(self):
        self.assertEqual(JsonHttpClient(Mock(return_value=io.BytesIO(b"")))("http://local"), {})


class TestTelegramAdapter(unittest.TestCase):
    def test_normalization_is_transport_neutral(self):
        message = TelegramAdapter.normalize("7", {
            "message_id": 12, "chat": {"id": -42}, "from": {"id": 3}, "text": " hi "})
        self.assertEqual(message.conversation, ConversationKey("telegram", "7", "-42"))
        self.assertEqual((message.message_id, message.sender_id, message.text), ("12", "3", "hi"))

    def test_caption_and_voice_without_text(self):
        base = {"message_id": 1, "chat": {"id": 2}, "from": {"id": 3}}
        self.assertEqual(TelegramAdapter.normalize("7", {**base, "caption": " file "}).text, "file")
        self.assertEqual(TelegramAdapter.normalize("7", base).text, "")

    def test_send_uses_telegram_wire_format(self):
        transport = Mock(return_value={"ok": True})
        adapter = TelegramAdapter(TelegramClient(transport), "private-token")
        self.assertEqual(adapter.send_text("42", "hello"), {"ok": True})
        transport.assert_called_once_with(
            "https://api.telegram.org/botprivate-token/sendMessage", "POST",
            {"chat_id": "42", "text": "hello"}, timeout=45)
        self.assertTrue(adapter.capabilities.edit_messages)


class TestOpenCodeAdapter(unittest.TestCase):
    def test_session_creation_keeps_permissions_and_location(self):
        transport = Mock(return_value={"data": {"id": "ses-1"}})
        client = OpenCodeClient("http://local/", ("name", "password"), transport)
        rules = [{"action": "shell", "resource": "*", "effect": "ask"}]
        self.assertEqual(client.create_session("job", "/workspace", rules), "ses-1")
        transport.assert_called_once_with("http://local/api/session", "POST", {
            "title": "job", "location": {"directory": "/workspace"}, "permissions": rules},
            ("name", "password"), timeout=45)

    def test_failed_prompt_is_reported_not_polled_as_success(self):
        client = OpenCodeClient("http://local", (), Mock(return_value={"error": 503}))
        with self.assertRaises(RuntimeError):
            client.submit_prompt("ses-1", "hello")

    def test_missing_session_id_is_not_cached_as_none(self):
        client = OpenCodeClient("http://local", (), Mock(return_value={}))
        with self.assertRaises(RuntimeError):
            client.create_session("job", "/workspace", [])

    def test_session_ids_are_escaped_and_external_paths_rejected(self):
        self.assertEqual(OpenCodeClient.session_path("a/b"), "/api/session/a%2Fb")
        client = OpenCodeClient("http://local", (), Mock())
        with self.assertRaises(ValueError):
            client.request("https://external.invalid")

    def test_messages(self):
        transport = Mock(return_value={"data": [{"id": "message-1"}]})
        client = OpenCodeClient("http://local", (), transport)
        self.assertEqual(client.messages("ses-1"), [{"id": "message-1"}])
