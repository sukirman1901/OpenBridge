"""Reconnectable SSE hints. Durable HTTP history remains the completion authority."""
import base64
import json
import threading
import urllib.request


class SessionEvents:
    def __init__(self, url, auth, log, opener=urllib.request.urlopen):
        self.url, self.auth, self.log, self.opener = url, auth, log, opener
        self.lock = threading.RLock()
        self.watched = {}
        self.connected = threading.Event()
        self.updates = 0

    def watch(self, sid):
        with self.lock:
            item = self.watched.setdefault(sid, {'event': threading.Event(), 'stage': 'working', 'refs': 0})
            item['refs'] += 1

    def unwatch(self, sid):
        with self.lock:
            item = self.watched.get(sid)
            if item:
                item['refs'] -= 1
                if item['refs'] <= 0:
                    del self.watched[sid]

    def stage(self, sid):
        with self.lock:
            item = self.watched.get(sid)
            return item['stage'] if item else None

    def wait(self, sid, timeout):
        with self.lock:
            item = self.watched.get(sid)
        if item is None:
            return False
        ready = item['event'].wait(timeout)
        item['event'].clear()
        return ready

    def feed(self, event):
        if not isinstance(event, dict) or not isinstance(event.get('data'), dict):
            return
        data, kind = event['data'], event.get('type', '')
        sid = data.get('sessionID')
        with self.lock:
            item = self.watched.get(sid)
            if not item:
                return
            if kind == 'permission.asked':
                item['stage'] = 'waiting_approval'
            elif kind == 'permission.replied' or kind.startswith('session.execution.'):
                item['stage'] = 'working'
            # Never retain deltas, reasoning, tool payloads, or terminal claims.
            if kind.startswith(('session.', 'permission.')):
                self.updates += 1
                item['event'].set()

    def consume(self, response, stopped):
        parts = []
        size = 0
        while not stopped.is_set():
            raw = response.readline(65536)
            if not raw:
                return
            line = raw.decode('utf-8', 'replace').rstrip('\r\n')
            if line.startswith('data:'):
                size += len(line)
                if size <= 1024 * 1024:
                    parts.append(line[5:].lstrip())
            elif not line:
                if parts and size <= 1024 * 1024:
                    try:
                        payload = json.loads('\n'.join(parts))
                        if isinstance(payload, str):
                            payload = json.loads(payload)
                        self.feed(payload)
                    except (ValueError, TypeError):
                        pass
                parts, size = [], 0

    def run(self, stopped):
        token = base64.b64encode(f'{self.auth[0]}:{self.auth[1]}'.encode()).decode()
        last_error = None
        while not stopped.is_set():
            try:
                request = urllib.request.Request(self.url + '/api/event', headers={
                    'Authorization': 'Basic ' + token, 'Accept': 'text/event-stream'})
                with self.opener(request, timeout=3) as response:
                    self.connected.set()
                    self.consume(response, stopped)
            except Exception as error:
                # Quiet streams time out and reconnect normally; never leak a
                # credential-bearing URL or flood logs every few seconds.
                name = type(error).__name__
                if name != last_error and name not in ('TimeoutError', 'SocketTimeout'):
                    self.log('Execution event stream reconnecting', name)
                last_error = name
            stopped.wait(1)
