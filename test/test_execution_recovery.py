"""Crash/reconnect recovery uses durable receipts, never another prompt POST."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from openbridge.adapters.persistence.sqlite.jobs import JobStore
from openbridge.domain.messages import ConversationKey
from openbridge.services.reconciliation import ExecutionReconciler


class ExecutionRecovery(unittest.TestCase):
    def test_active_old_receipts_do_not_starve_later_completed_jobs(self):
        self.receipt()
        self.interrupt()
        for identity in range(2, 36):
            job, _ = self.store.accept(ConversationKey('telegram', '123', str(identity)), identity, {})
            self.store.acknowledge(job)
            self.store.claim(job)
            receipt = self.store.begin_prompt(job, f'sid-{identity}', 'reply')
            self.store.accept_prompt(receipt, f'user-{identity}')
            self.store.finish(job, [], status='interrupted')
        self.backend.session_idle.side_effect = lambda sid: sid == 'sid-35'
        reconciler = ExecutionReconciler(self.store, self.backend, Mock())
        reconciler.tick()
        reconciler.tick()
        self.assertEqual(self.store.get(35)['status'], 'succeeded')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'jobs.db')
        self.store = JobStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.job, _ = self.store.accept(ConversationKey('telegram', '123', '42'), 1, {})
        self.store.acknowledge(self.job, 99)
        self.store.claim(self.job)
        self.backend = Mock()
        self.backend.session_idle.return_value = True
        self.backend.completion.return_value = {'outcome': 'succeeded', 'answer': 'Final answer'}

    def receipt(self, purpose='reply', accepted=True):
        self.store.checkpoint(self.job, 'sid')
        receipt = self.store.begin_prompt(self.job, 'sid', purpose)
        if accepted:
            self.store.accept_prompt(receipt, 'user-current')
        return receipt

    def interrupt(self):
        self.store.finish(self.job, [{'operation': 'send', 'text': 'Needs review'}],
                          status='interrupted', error='ExecutionUncertain')
        notice = self.store.claim_delivery()
        self.store.delivery_result(notice, 'sent')

    def reconcile(self):
        ExecutionReconciler(self.store, self.backend, Mock()).tick()

    def test_receipt_survives_restart_and_releases_fence_without_replay(self):
        self.receipt()
        self.store.close()
        self.store = JobStore(self.path)
        self.store.recover()
        notice = self.store.claim_delivery()
        self.store.delivery_result(notice, 'sent')
        self.reconcile()
        self.assertEqual(self.store.get(self.job)['status'], 'succeeded')
        self.backend.completion.assert_called_once_with('sid', 'user-current')
        self.backend.run_prompt.assert_not_called()
        self.backend.submit_prompt.assert_not_called()
        self.assertEqual(self.store.claim_delivery()['payload']['message_id'], 99)

    def test_active_or_unknown_backend_never_releases_fence(self):
        self.receipt()
        self.interrupt()
        self.backend.session_idle.return_value = False
        observe = Mock()
        ExecutionReconciler(self.store, self.backend, Mock(), observe=observe).tick()
        self.assertEqual(observe.call_args.args[0]['session_id'], 'sid')
        self.assertEqual(self.store.get(self.job)['status'], 'interrupted')
        self.backend.completion.assert_not_called()

    def test_missing_acceptance_receipt_cannot_be_guessed_from_history(self):
        self.receipt(accepted=False)
        self.interrupt()
        self.reconcile()
        self.assertEqual(self.store.get(self.job)['status'], 'interrupted')
        self.backend.completion.assert_not_called()

    def test_nonterminal_result_cannot_release_fence(self):
        self.receipt()
        self.interrupt()
        self.backend.completion.return_value = None
        self.reconcile()
        self.assertEqual(self.store.get(self.job)['status'], 'interrupted')

    def test_reconciliation_is_idempotent_and_preserves_file_outputs(self):
        self.receipt()
        self.interrupt()
        self.backend.completion.return_value = {'outcome': 'succeeded', 'answer': 'Here.\n@file hasil/report.pdf'}
        self.reconcile()
        self.reconcile()
        outputs = json.loads(self.store.get(self.job)['result'])
        self.assertEqual(outputs[-1]['operation'], 'document')
        count = self.store.db.execute('SELECT COUNT(*) FROM runtime_deliveries WHERE job_id=?', (self.job,)).fetchone()[0]
        self.assertEqual(count, 3)  # review notice, final text, file
        from openbridge.services.job_commands import JobCommands
        send = Mock()
        JobCommands(self.store, self.backend, send, channel='telegram').handle('123', 42, f'/result {self.job}')
        self.assertIn('hasil/report.pdf', send.call_args.args[0])

    def test_failed_backend_releases_fence_as_failed_not_replayed(self):
        self.receipt()
        self.interrupt()
        self.backend.completion.return_value = {'outcome': 'failed', 'answer': ''}
        self.reconcile()
        self.assertEqual(self.store.get(self.job)['status'], 'failed')
        self.backend.run_prompt.assert_not_called()

    def test_briefing_completion_is_not_mistaken_for_a_user_reply(self):
        self.receipt(purpose='briefing')
        self.interrupt()
        self.reconcile()
        job = self.store.get(self.job)
        self.assertEqual(job['status'], 'failed')
        self.assertNotIn('Final answer', job['result'])

    def test_later_unreceipted_submission_keeps_fence(self):
        self.receipt()
        self.store.begin_prompt(self.job, 'sid', 'reply')
        self.interrupt()
        self.reconcile()
        self.assertEqual(self.store.get(self.job)['status'], 'interrupted')

    def test_live_running_job_is_not_settled_by_reconciler(self):
        self.receipt()
        self.reconcile()
        self.assertEqual(self.store.get(self.job)['status'], 'running')
        self.backend.completion.assert_not_called()

    def test_completed_task_step_is_checkpointed_without_running_remaining_steps(self):
        from task_queue import TaskQueue
        tasks = TaskQueue(self.path)
        self.addCleanup(tasks.close)
        task = tasks.add_task('123', 42, 'make report', max_steps=3)
        self.job = self.store.enqueue_task(task, '123')
        self.store.claim(self.job)
        receipt = self.store.begin_prompt(self.job, 'sid', 'task_step', step=0)
        self.store.accept_prompt(receipt, 'u-task')
        self.store.finish(self.job, [], status='interrupted', error='ExecutionUncertain')
        self.backend.completion.return_value = {'outcome': 'succeeded', 'answer': 'Collected data; next step needed'}
        self.reconcile()
        self.assertEqual(self.store.get(self.job)['status'], 'failed')
        self.assertEqual(self.store.completed_steps(self.job)[0]['answer'], 'Collected data; next step needed')
        self.backend.run_prompt.assert_not_called()
