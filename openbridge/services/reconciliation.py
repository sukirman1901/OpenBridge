"""Read-only backend recovery of exactly receipted executions, never retries."""
import re

from openbridge.services.background import parse_file_outputs


class ExecutionReconciler:
    def __init__(self, store, backend, log, observe=None):
        self.store, self.backend, self.log = store, backend, log
        self.observe = observe

    def tick(self):
        for receipt in self.store.interrupted_prompts():
            try:
                if self.observe:
                    self.observe(receipt)  # Re-register approval routing after restart.
                self._settle(receipt)
            except Exception as error:
                # A disconnected GET leaves the fence in place, never submits.
                self.log('Reconciliation lookup failed', receipt['job_id'], type(error).__name__)

    def _settle(self, receipt):
        sid = receipt['session_id']
        if not self.backend.session_idle(sid):
            return
        result = self.backend.completion(sid, receipt['message_id'])
        if not isinstance(result, dict) or result.get('outcome') not in ('succeeded', 'failed'):
            return
        # Recheck after reading history. Events and message text alone cannot
        # authorize releasing a conversation with active/queued backend work.
        if not self.backend.session_idle(sid):
            return
        status = result['outcome']
        answer = result.get('answer') or ''
        step_answer = answer if receipt['purpose'] == 'task_step' and status == 'succeeded' else None
        purpose = receipt['purpose']
        if purpose == 'compaction':
            answer = '✅ Session maintenance finished. Chat can continue.' if status == 'succeeded' else '⚠️ Session maintenance stopped. Chat can continue.'
        elif purpose in ('briefing', 'handoff'):
            status = 'failed'
            answer = '⚠️ Session preparation finished after a disconnect. Your request was not replayed; please send it again.'
        elif status == 'failed':
            answer = '⚠️ The backend stopped this request. It was not replayed. You can send a new request.'
        elif purpose == 'task_step':
            marker = re.match(r'\s*(?:\*\*|__)?(DONE|SELESAI)\b', answer, re.I)
            if not marker:
                status = 'failed'
                answer = ('⚠️ A task step finished after a disconnect, but the task is not confirmed complete. '
                          'It was not replayed. Saved step result:\n' + answer)
        elif purpose not in ('reply', 'schedule', 'initiative'):
            return  # Unknown purposes cannot be interpreted safely.
        if not answer.strip():
            status = 'failed'
            answer = '⚠️ The backend finished without a usable result. It was not replayed.'
        if purpose == 'initiative' and answer.strip().upper() in ('SILENT', 'DIAM'):
            answer = ''
        text, files = parse_file_outputs(answer)
        outputs = [{'operation': 'send', 'text': text[i:i + 3500], 'parse': False}
                   for i in range(0, len(text), 3500)]
        outputs.extend(files)
        if self.store.reconcile_completed(receipt['job_id'], outputs, status=status,
                                           error=None if status == 'succeeded' else 'RecoveredBackendFailure',
                                           prompt_id=receipt['id'], step_answer=step_answer):
            self.log('Receipted execution reconciled without replay', receipt['job_id'], status)
