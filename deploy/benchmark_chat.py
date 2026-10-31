"""Opt-in three-message live latency check using an isolated DB/session.

Sends three short Telegram notices to the configured owner. It never polls
Telegram or submits to the production conversation's OpenCode session.
"""
import argparse
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import uuid
import sys

from openbridge.bootstrap import create_application
from openbridge.config import Configuration, Paths, WorkerSettings, load_config
from openbridge.adapters.execution.opencode.client import OpenCodeClient, load_service
from openbridge.adapters.http import JsonHttpClient
from openbridge.adapters.channels.telegram.adapter import TelegramClient


class BenchmarkFailure(RuntimeError):
    def __init__(self, report):
        super().__init__('Live benchmark was not confirmed; do not blindly retry sends')
        self.report = report


def benchmark(home, service):
    source = load_config(Paths(home, service))
    data = copy.deepcopy(source.data)
    data['bots'] = data['bots'][:1]
    data['bots'][0]['nama'] = 'Latency audit ' + uuid.uuid4().hex[:8]
    data['bots'][0]['persona'] = 'Follow direct test requests; reply briefly. Never use tools.'
    data['budget'] = {'max_call_per_hari': 100, 'max_cost_hari': 1}
    data['rate_limiter'] = {'max_per_menit': 100}
    owner = data['owner_ids'][0]
    url, auth = load_service(service)
    results, session_ids = [], set()
    channel = TelegramClient(JsonHttpClient())
    phase = ['cold']
    calls, logs = [], []
    log = lambda *parts: logs.append(' '.join(str(part) for part in parts))
    def timed_call(token, method, payload=None, **options):
        label, start = phase[0], time.monotonic()
        result = channel.call(token, method, payload, **options)
        calls.append({'phase': label, 'method': method, 'seconds': round(time.monotonic() - start, 3),
                      'ok': bool(result.get('ok')), 'error': result.get('error_code') or result.get('error'),
                      'parse_mode': bool((payload or {}).get('parse_mode'))})
        return result
    with tempfile.TemporaryDirectory(prefix='chat-latency-', dir='/tmp/opencode') as directory:
        root = Path(directory)
        (root / 'opencode.jsonc').write_text(json.dumps({'permissions': [
            {'action': '*', 'resource': '*', 'effect': 'deny'}]}))
        configuration = Configuration(Paths(root, service), data, WorkerSettings.from_config(data))
        app = None
        try:
            for identity, label in enumerate(('cold', 'warm', 'after_restart'), 1):
                phase[0] = label
                if identity in (1, 3):
                    if app:
                        app.close()
                    app = create_application(configuration,
                        backend=OpenCodeClient(url, auth, JsonHttpClient()), channel_call=timed_call, log=log)
                    p = app.components
                    p.presence.start()
                    thread = threading.Thread(target=p.backend.events.run, args=(app.shutdown,), daemon=True)
                    app.loop_threads.append(thread)
                    thread.start()
                p = app.components
                started = time.monotonic()
                p.gateway.accept(data['bots'][0], {'update_id': identity, 'message': {
                    'message_id': identity, 'chat': {'id': owner, 'type': 'private'},
                    'from': {'id': owner, 'first_name': 'Runtime latency check'},
                    'text': f'Reply exactly LATENCY_AUDIT_OK_{identity}. Do not use tools.'}})
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    p.runtime.pump()
                    p.runtime.deliver_one()
                    with p.jobs.lock:
                        job = p.jobs.get(identity)
                        deliveries = p.jobs.db.execute('SELECT status FROM runtime_deliveries WHERE job_id=?',
                                                       (identity,)).fetchall()
                    if job['status'] in ('failed', 'interrupted'):
                        raise RuntimeError('Live benchmark did not complete successfully')
                    if job['status'] == 'queued' and time.monotonic() > started + 10:
                        raise RuntimeError('Backend deferred the benchmark job (runtime/provider limit); '
                                           'no blind retry')
                    if any(d[0] in ('dead', 'uncertain') for d in deliveries):
                        raise RuntimeError('Delivery outcome not confirmed; no automatic resend')
                    if job['status'] == 'succeeded' and deliveries and all(d[0] == 'sent' for d in deliveries):
                        break
                    time.sleep(.01)
                else:
                    raise RuntimeError('Benchmark deadline expired; do not blindly retry sends')
                if f'LATENCY_AUDIT_OK_{identity}' not in (job['result'] or ''):
                    raise RuntimeError('Benchmark response did not match the requested check')
                run = p.jobs.db.execute('SELECT * FROM runtime_runs WHERE job_id=?', (identity,)).fetchone()
                receipts = p.jobs.db.execute('SELECT * FROM runtime_prompts WHERE job_id=? ORDER BY id', (identity,)).fetchall()
                reply = next(r for r in receipts if r['purpose'] == 'reply')
                session_ids.add(reply['session_id'])
                results.append({'phase': label, 'end_to_end_s': round(time.monotonic() - started, 3),
                    'queue_s': round(run['started_at'] - job['created_at'], 3),
                    'before_prompt_s': round(reply['created_at'] - run['started_at'], 3),
                    'backend_wait_s': round(reply['updated_at'] - reply['created_at'], 3),
                    'briefing_calls': sum(r['purpose'] == 'briefing' for r in receipts),
                    'delivery_attempts': p.jobs.db.execute('SELECT SUM(attempts) FROM runtime_deliveries WHERE job_id=?',
                                                         (identity,)).fetchone()[0],
                    'telegram_calls': [c for c in calls if c['phase'] == label]})
        except Exception as error:
            diagnostics = {'phase': phase[0], 'error_type': type(error).__name__,
                           'telegram_calls': [c for c in calls if c['phase'] == phase[0]],
                           'bridge_log': logs[-40:]}
            if app:
                with app.components.jobs.lock:
                    diagnostics['jobs'] = [dict(r) for r in app.components.jobs.db.execute('SELECT id,status,error,ack_ready,next_try,lane,kind FROM runtime_jobs')]
                    diagnostics['deliveries'] = [dict(r) for r in app.components.jobs.db.execute('SELECT status,error,attempts FROM runtime_deliveries')]
                diagnostics['presence_size'] = app.components.presence.size
                diagnostics['scheduled'] = list(getattr(app.components.runtime, 'scheduled', []))
            results.append(diagnostics)
            raise BenchmarkFailure(results) from None
        finally:
            if app:
                app.close()
            cleanup = OpenCodeClient(url, auth, JsonHttpClient())
            for sid in session_ids:
                if cleanup.session_idle(sid):
                    cleanup.delete_session(sid)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--service', type=Path, default=Path.home() / '.local/state/opencode/service.json')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    failed = False
    try:
        data = benchmark(args.home.resolve(), args.service)
    except BenchmarkFailure as error:
        failed = True
        data = error.report
    report = json.dumps(data, indent=2)
    if args.output:
        args.output.write_text(report + '\n')
    print(report)
    if failed:
        sys.exit(1)
