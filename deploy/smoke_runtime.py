"""Opt-in live smoke: one harmless prompt, persisted receipt, late-result recovery.

Run from the repository with python3 -m deploy.smoke_runtime. Add --telegram
to send one successful-check notice to the first configured owner. This never
submits work to an existing user session or modifies the production job DB.
"""
import argparse
from pathlib import Path
import tempfile
import threading
import time

from openbridge.adapters.channels.telegram.adapter import TelegramClient
from openbridge.adapters.channels.telegram.delivery import TelegramDelivery
from openbridge.adapters.execution.opencode.client import OpenCodeClient, load_service
from openbridge.adapters.execution.opencode.events import SessionEvents
from openbridge.adapters.http import JsonHttpClient
from openbridge.adapters.persistence.sqlite.jobs import JobStore
from openbridge.config import Paths, load_config
from openbridge.domain.messages import ConversationKey
from openbridge.runtime.jobs import DurableRuntime
from openbridge.services.reconciliation import ExecutionReconciler
from runtime_policy import bot_id


def smoke(home, service, telegram=False, timeout=90):
    transport = JsonHttpClient()
    posts = []
    def counted(url, method='GET', *args, **kwargs):
        if method == 'POST' and url.endswith('/prompt'):
            posts.append(url)
        return transport(url, method, *args, **kwargs)
    url, auth = load_service(service)
    client = OpenCodeClient(url, auth, counted)
    log = lambda *args: None
    stopped = threading.Event()
    events = SessionEvents(url, auth, log)
    client.events = events
    thread = threading.Thread(target=events.run, args=(stopped,), daemon=True)
    thread.start()
    sid = None
    try:
        if not events.connected.wait(8):
            raise RuntimeError('Live SSE connection was not confirmed')
        with tempfile.TemporaryDirectory(prefix='runtime-smoke-', dir='/tmp/opencode') as directory:
            root = Path(directory)
            sid = client.create_session('OpenBridge recovery smoke (no tools)', str(root),
                [{'action': '*', 'resource': '*', 'effect': 'deny'}])
            events.watch(sid)
            path = str(root / 'smoke.db')
            store = JobStore(path)
            try:
                job, _ = store.accept(ConversationKey('smoke', 'local', 'check'), 1, {})
                store.acknowledge(job)
                def execute(job, payload):
                    runtime.checkpoint(sid)
                    runtime.run_prompt(client, sid, 'Reply exactly RUNTIME_RECOVERY_OK. Do not use tools.', timeout=0)
                runtime = DurableRuntime(store, None, execute, None, log)
                runtime.run(job)
                if store.get(job)['status'] != 'interrupted':
                    raise RuntimeError('Timeout injection did not interrupt the local wait')
                if not store.interrupted_prompts():
                    raise RuntimeError('Accepted message ID was not persisted')
            finally:
                store.close()
            store = JobStore(path)  # Separate connection, as after process restart.
            try:
                store.recover()
                reconciler = ExecutionReconciler(store, client, log)
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline and store.get(job)['status'] == 'interrupted':
                    reconciler.tick()
                    stopped.wait(.5)
                result = store.get(job)
                if result['status'] != 'succeeded' or 'RUNTIME_RECOVERY_OK' not in (result['result'] or ''):
                    raise RuntimeError('Live late result was not recovered successfully')
                if len(posts) != 1:
                    raise RuntimeError('Prompt POST was replayed')
                print('LIVE PASS: durable receipt, reopen, exact late result, one prompt POST, SSE connected.')
                print('Observed session events:', events.updates)
                if telegram:
                    cfg = load_config(Paths(home, service)).data
                    bot, owner = cfg['bots'][0], cfg['owner_ids'][0]
                    channel = TelegramDelivery(TelegramClient(transport).call, lambda account: bot, None, log)
                    outcome = channel.deliver({'id': 0, 'job_id': 0, 'account_id': bot_id(bot),
                        'conversation_id': str(owner), 'payload': {'operation': 'send',
                        'text': '✅ Runtime safety check: hasil terlambat berhasil dipulihkan tanpa mengulang pekerjaan.'}})
                    if outcome[0] != 'sent':
                        raise RuntimeError('Telegram smoke outcome not confirmed; do not blindly retry')
                    print('LIVE PASS: Telegram delivery confirmed.')
            finally:
                store.close()
    finally:
        stopped.set()
        thread.join(5)
        if sid and client.session_idle(sid):
            client.delete_session(sid)  # Only the isolated smoke session created above.


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--service', type=Path, default=Path.home() / '.local/state/opencode/service.json')
    parser.add_argument('--telegram', action='store_true')
    args = parser.parse_args()
    smoke(args.home.resolve(), args.service, args.telegram)
