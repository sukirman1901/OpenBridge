import unittest
from unittest.mock import Mock

from openbridge.adapters.execution.opencode import OpenCodeClient
from openbridge.domain.errors import ExecutionUncertain, RetryLater


class TestOpenCodeCompletion(unittest.TestCase):
    def test_tracked_terminal_result_waits_for_session_drain_release(self):
        client = self.client([{'data': []}])
        client.messages = Mock(return_value=self.messages()['data'])
        client.session_idle = Mock(side_effect=[False, True])
        wait = Mock()
        self.assertEqual(client.run_prompt('session', 'hello', on_state=Mock(), wait=wait), 'final')
        self.assertEqual(client.session_idle.call_count, 2)
        wait.assert_called_once_with(1)

    def test_compaction_lookup_failure_remains_ambiguous_after_acceptance(self):
        client = OpenCodeClient('http://local', (), Mock(side_effect=[
            {'data': {}}, {'data': {'id': 'compact-1'}}, {'error': 503}]))
        receipt = Mock()
        with self.assertRaises(ExecutionUncertain):
            client.compact_session('session', on_accepted=receipt, wait=Mock())
        receipt.assert_called_once_with('compact-1')

    def test_compaction_waits_for_its_exact_terminal_receipt(self):
        transport = Mock(side_effect=[{'data': {}}, {'data': {'id': 'compact-1'}},
            {'data': [{'id': 'compact-1', 'type': 'compaction', 'time': {'created': 1}}]},
            {'data': [{'id': 'compact-1', 'type': 'compaction', 'time': {'created': 1}},
                      {'id': 'idle', 'type': 'idle', 'outcome': 'succeeded', 'time': {'created': 2}}]},
            {'data': {}}, {'data': []}])
        client = OpenCodeClient('http://local', (), transport)
        accepted, wait = Mock(), Mock()
        client.compact_session('session', on_accepted=accepted, wait=wait)
        accepted.assert_called_once_with('compact-1')
        wait.assert_called_once_with(1)

    def test_approval_wait_uses_separate_budget_from_tool_execution(self):
        now = [0]
        def transport(url, method='GET', body=None, auth=None, timeout=45):
            if url.endswith('/active'):
                return {'data': {}}
            if url.endswith('/prompt'):
                return {'data': {'id': 'current-user'}}
            if url.endswith('/permission'):
                return {'data': [{'id': 'permission-1'}] if now[0] < 4 else []}
            if url.endswith('/inbox'):
                return {'data': []}
            if url.endswith('/message'):
                return self.messages() if now[0] >= 5 else {'data': [
                    {'id': 'current-user', 'type': 'user', 'time': {'created': 10}}]}
            raise AssertionError(url)
        client = OpenCodeClient('http://local', (), transport)
        states = []
        answer = client.run_prompt('session', 'hello', timeout=2, approval_timeout=10,
            on_state=states.append, clock=lambda: now[0], wait=lambda n: now.__setitem__(0, now[0] + n))
        self.assertEqual(answer, 'final')
        self.assertIn('waiting_approval', states)
        self.assertGreater(now[0], 2)

    def test_permission_wait_is_bounded_and_never_auto_approved(self):
        now, calls = [0], []
        def transport(url, method='GET', body=None, auth=None, timeout=45):
            calls.append((url, method))
            if url.endswith('/active'):
                return {'data': {}}
            if url.endswith('/prompt'):
                return {'data': {'id': 'current-user'}}
            if url.endswith('/permission'):
                return {'data': [{'id': 'permission-1'}]}
            return {'data': [{'id': 'current-user', 'type': 'user', 'time': {'created': 10}}]}
        client = OpenCodeClient('http://local', (), transport)
        with self.assertRaises(ExecutionUncertain):
            client.run_prompt('session', 'hello', timeout=2, approval_timeout=3, on_state=Mock(),
                clock=lambda: now[0], wait=lambda n: now.__setitem__(0, now[0] + n))
        self.assertLessEqual(now[0], 5)
        self.assertEqual([method for _, method in calls if method == 'POST'], ['POST'])

    def test_receipt_is_persisted_before_any_result_lookup(self):
        client = self.client([self.messages()])
        receipt = Mock()
        def accepted(message_id):
            self.assertEqual(client.transport.call_count, 2)
            receipt(message_id)
        self.assertEqual(client.run_prompt('session', 'hello', on_accepted=accepted), 'final')
        receipt.assert_called_once_with('current-user')

    def test_receipt_storage_failure_is_ambiguous_not_retried(self):
        client = self.client([])
        with self.assertRaises(ExecutionUncertain):
            client.run_prompt('session', 'hello', on_accepted=Mock(side_effect=OSError('disk')))
        self.assertEqual(client.transport.call_count, 2)

    def test_completion_never_uses_a_later_users_answer(self):
        client = self.client([])
        client.messages = Mock(return_value=[
            {'id': 'u1', 'type': 'user', 'time': {'created': 1}},
            {'id': 'u2', 'type': 'user', 'time': {'created': 2}},
            {'id': 'a2', 'type': 'assistant', 'finish': 'stop', 'time': {'created': 3},
             'content': [{'type': 'text', 'text': 'wrong reply'}]},
            {'id': 'i2', 'type': 'idle', 'outcome': 'succeeded', 'time': {'created': 4}},
        ])
        self.assertIsNone(client.completion('session', 'u1'))
        self.assertEqual(client.completion('session', 'u2')['answer'], 'wrong reply')

    def test_disconnect_during_lookup_does_not_resubmit_prompt(self):
        client = self.client([{'error': 'URLError'}, self.messages()])
        self.assertEqual(client.run_prompt('session', 'hello', wait=Mock()), 'final')
        posts = [c for c in client.transport.call_args_list if len(c.args) > 1 and c.args[1] == 'POST']
        self.assertEqual(len(posts), 1)

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

    def test_quota_after_tool_work_never_replays_the_prompt(self):
        history = self.messages('failed')
        history['data'][0]['error'] = {'type': 'provider.chatgpt-limit'}
        history['data'].append({'id': 'tool-work', 'type': 'assistant', 'finish': 'tool-calls',
            'time': {'created': 15}, 'content': [{'type': 'tool', 'name': 'shell'}]})
        client = self.client([history])
        with self.assertRaises(RuntimeError) as error:
            client.run_prompt('session', 'hello')
        self.assertNotIsInstance(error.exception, RetryLater)

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
