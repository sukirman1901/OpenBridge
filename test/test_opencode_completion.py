import unittest
from unittest.mock import Mock

from openbridge.adapters.execution.opencode import OpenCodeClient
from openbridge.domain.errors import ExecutionUncertain, RetryLater


class TestOpenCodeCompletion(unittest.TestCase):
    def client(self, history, accepted=None, active=None):
        transport = Mock(side_effect=[{"data": active or {}},
            accepted if accepted is not None else {"data": {"id": "current-user"}}, *history])
        return OpenCodeClient("http://local", (), transport)

    def messages(self, outcome="succeeded"):
        # The real service returns newest-first, with idle after assistant completion.
        return {"data": [
            {"id": "idle", "type": "idle", "outcome": outcome, "time": {"created": 30}},
            {"id": "answer", "type": "assistant", "finish": "stop", "time": {"created": 20},
             "content": [{"type": "text", "text": "final"}]},
            {"id": "current-user", "type": "user", "time": {"created": 10}},
            {"id": "old-answer", "type": "assistant", "time": {"created": 5},
             "content": [{"type": "text", "text": "wrong"}]},
        ]}

    def test_matches_accepted_message_and_waits_for_terminal_idle(self):
        partial = {"data": [
            {"id": "current-user", "type": "user", "time": {"created": 10}},
            {"id": "answer", "type": "assistant", "time": {"created": 20},
             "content": [{"type": "text", "text": "partial"}]},
        ]}
        client = self.client([partial, self.messages()])
        wait = Mock()
        self.assertEqual(client.run_prompt("session", "hello", wait=wait), "final")
        wait.assert_called_once_with(1)

    def test_active_session_is_not_given_another_prompt(self):
        client = self.client([], active={"session": {"type": "running"}})
        with self.assertRaises(ExecutionUncertain):
            client.run_prompt("session", "hello")
        self.assertEqual(client.transport.call_count, 1)

    def test_missing_receipt_or_ambiguous_acceptance_requires_review(self):
        for receipt in ({}, {"error": 503}, {"error": "URLError"}, {"error": 409}):
            with self.subTest(receipt=receipt):
                with self.assertRaises(ExecutionUncertain):
                    self.client([], receipt).run_prompt("session", "hello")

    def test_known_rejection_does_not_claim_success(self):
        with self.assertRaises(RuntimeError) as error:
            self.client([], {"error": 400}).run_prompt("session", "hello")
        self.assertNotIsInstance(error.exception, ExecutionUncertain)

    def test_timeout_does_not_return_partial_text(self):
        client = self.client([])
        with self.assertRaises(ExecutionUncertain):
            client.run_prompt("session", "hello", timeout=0)

    def test_terminal_failure_is_not_an_ambiguous_running_execution(self):
        client = self.client([self.messages("failed")])
        with self.assertRaises(RuntimeError) as error:
            client.run_prompt("session", "hello")
        self.assertNotIsInstance(error.exception, ExecutionUncertain)

    def test_provider_limit_outcome_defers_with_quota_pause(self):
        from openbridge.domain.errors import RetryLater
        history = self.messages("failed")
        history["data"][0]["error"] = {"type": "provider.chatgpt-limit",
                                       "message": "ChatGPT usage limit reached."}
        client = self.client([history])
        with self.assertRaises(RetryLater) as error:
            client.run_prompt("session", "hello")
        self.assertGreaterEqual(error.exception.delay, 60)

    def test_plain_provider_failure_still_ends_the_job(self):
        history = self.messages("failed")
        history["data"][0]["error"] = {"type": "provider.internal", "message": "boom"}
        client = self.client([history])
        with self.assertRaises(RuntimeError) as error:
            client.run_prompt("session", "hello")
        self.assertNotIsInstance(error.exception, RetryLater)

    def test_terminal_failure_without_error_details_still_ends_the_job(self):
        client = self.client([self.messages("failed")])
        with self.assertRaises(RuntimeError) as error:
            client.run_prompt("session", "hello")
        self.assertNotIsInstance(error.exception, RetryLater)

    def test_transport_failure_after_acceptance_requires_review(self):
        client = self.client([{"error": 503}])
        with self.assertRaises(ExecutionUncertain):
            client.run_prompt("session", "hello")

    def test_aborted_assistant_without_idle_does_not_fence_an_idle_session(self):
        history = {"data": [
            {"id": "current-user", "type": "user", "time": {"created": 10}},
            {"id": "failed", "type": "assistant", "finish": "error",
             "error": {"type": "aborted"}, "time": {"created": 20}},
        ]}
        client = self.client([history])
        client.session_idle = Mock(return_value=True)
        with self.assertRaises(RuntimeError) as error:
            client.run_prompt("session", "hello", wait=Mock())
        self.assertNotIsInstance(error.exception, ExecutionUncertain)
        client.session_idle.assert_called_once_with("session")

    def test_aborted_assistant_with_unverified_backend_still_requires_review(self):
        history = {"data": [
            {"id": "current-user", "type": "user", "time": {"created": 10}},
            {"id": "failed", "type": "assistant", "finish": "error",
             "error": {"type": "aborted"}, "time": {"created": 20}},
        ]}
        client = self.client([history])
        client.session_idle = Mock(return_value=False)
        with self.assertRaises(ExecutionUncertain):
            client.run_prompt("session", "hello", wait=Mock())
        client.session_idle.assert_called_once_with("session")
