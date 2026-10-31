"""Operator reconciliation of a verified late result never re-executes work."""
import unittest

from openbridge.adapters.persistence.sqlite.jobs import JobStore
from openbridge.domain.messages import ConversationKey


class LateResult(unittest.TestCase):
    def test_verified_result_settles_interrupted_job_and_queues_final_edit(self):
        store = JobStore()
        self.addCleanup(store.close)
        job, _ = store.accept(ConversationKey('telegram', '123', '42'), 1, {})
        store.acknowledge(job, 99)
        store.claim(job)
        store.finish(job, [{'operation': 'send', 'text': 'Needs review'}],
                     status='interrupted', error='ExecutionUncertain')
        notice = store.claim_delivery()
        store.delivery_result(notice, 'sent')
        outputs = [{'operation': 'send', 'text': 'Verified final result'}]
        self.assertTrue(store.reconcile_completed(job, outputs))
        self.assertEqual(store.get(job)['status'], 'succeeded')
        self.assertFalse(store.reconcile_completed(job, outputs))
        delivery = store.claim_delivery()
        self.assertEqual(delivery['payload']['operation'], 'edit')
        self.assertEqual(delivery['payload']['message_id'], 99)
        self.assertEqual(delivery['payload']['text'], 'Verified final result')

    def test_running_job_cannot_be_reconciled_as_completed(self):
        store = JobStore()
        self.addCleanup(store.close)
        job, _ = store.accept(ConversationKey('telegram', '123', '42'), 1, {})
        store.acknowledge(job)
        store.claim(job)
        self.assertFalse(store.reconcile_completed(job, []))
        self.assertEqual(store.get(job)['status'], 'running')
