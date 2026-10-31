"""HTTP fault-injection smoke: lost responses, persisted receipts, restart, late result."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from openbridge.adapters.execution.opencode.client import OpenCodeClient
from openbridge.adapters.http import JsonHttpClient
from openbridge.adapters.persistence.sqlite.jobs import JobStore
from openbridge.domain.messages import ConversationKey
from openbridge.runtime.jobs import DurableRuntime
from openbridge.services.reconciliation import ExecutionReconciler


class RuntimeFaults(unittest.TestCase):
    def test_real_http_disconnect_and_restart_recovers_exact_result_with_one_post(self):
        state = {'posts': 0, 'ready': False}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body, code=200):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                state['posts'] += 1
                self.reply({'data': {'id': 'accepted-one'}})

            def do_GET(self):
                if self.path.endswith('/active'):
                    self.reply({'data': {} if state['ready'] or not state['posts'] else {'sid': {'type': 'running'}}})
                elif self.path.endswith('/permission') or self.path.endswith('/inbox'):
                    self.reply({'data': []})
                elif not state['ready']:
                    self.reply({'error': 'temporary disconnect'}, 503)
                else:
                    self.reply({'data': [
                        {'id': 'accepted-one', 'type': 'user', 'time': {'created': 1}},
                        {'id': 'a', 'type': 'assistant', 'finish': 'stop', 'time': {'created': 2},
                         'content': [{'type': 'text', 'text': 'DONE. Actual late result'}]},
                        {'id': 'idle', 'type': 'idle', 'outcome': 'succeeded', 'time': {'created': 3}},
                    ]})
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / 'state.db')
                store = JobStore(path)
                client = OpenCodeClient(f'http://127.0.0.1:{server.server_port}', (), JsonHttpClient())
                job, _ = store.accept(ConversationKey('telegram', '123', '42'), 1, {})
                store.acknowledge(job, 9)
                def execute(j, payload):
                    runtime.checkpoint('sid')
                    runtime.run_prompt(client, 'sid', 'one request', timeout=1)
                runtime = DurableRuntime(store, Mock(), execute, Mock(), Mock())
                runtime.run(job)
                self.assertEqual(store.get(job)['status'], 'interrupted')
                store.close()
                state['ready'] = True
                store = JobStore(path)
                try:
                    store.recover()
                    reconciler = ExecutionReconciler(store, client, Mock())
                    reconciler.tick()
                    reconciler.tick()
                    self.assertEqual(state['posts'], 1)
                    self.assertEqual(store.get(job)['status'], 'succeeded')
                    self.assertIn('Actual late result', store.get(job)['result'])
                finally:
                    store.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
