import unittest
from unittest.mock import Mock

from openbridge.adapters.persistence.sqlite.jobs import JobStore
from openbridge.domain.errors import ExecutionUncertain
from openbridge.domain.messages import ConversationKey
from openbridge.runtime.jobs import DurableRuntime


class PromptTracking(unittest.TestCase):
    def test_submission_records_intent_then_receipt_before_waiting(self):
        store = JobStore()
        self.addCleanup(store.close)
        job, _ = store.accept(ConversationKey('telegram', '123', '42'), 1, {})
        store.acknowledge(job)
        backend = Mock()
        def prompt(sid, text, **options):
            row = store.db.execute('SELECT * FROM runtime_prompts').fetchone()
            self.assertIsNone(row['message_id'])
            options['on_accepted']('u-current')
            options['on_state']('waiting_approval')
            row = store.db.execute('SELECT * FROM runtime_prompts').fetchone()
            self.assertEqual(row['message_id'], 'u-current')
            self.assertEqual(row['stage'], 'waiting_approval')
            raise ExecutionUncertain('network')
        backend.run_prompt.side_effect = prompt
        runtime = DurableRuntime(store, Mock(), lambda j, p: runtime.run_prompt(backend, 'sid', 'hello'), Mock(), Mock())
        runtime.run(job)
        self.assertEqual(store.get(job)['status'], 'interrupted')
        self.assertEqual(store.interrupted_prompts()[0]['message_id'], 'u-current')

    def test_drain_prevents_new_claims_without_losing_accepted_jobs(self):
        store = JobStore()
        self.addCleanup(store.close)
        job, _ = store.accept(ConversationKey('telegram', '123', '42'), 1, {})
        store.acknowledge(job)
        execute = Mock()
        runtime = DurableRuntime(store, Mock(), execute, Mock(), Mock(), drain_requested=lambda: True)
        runtime.run(job)
        self.assertEqual(store.get(job)['status'], 'queued')
        execute.assert_not_called()
        store.claim_delivery = Mock()
        self.assertFalse(runtime.deliver_one())
        store.claim_delivery.assert_not_called()
