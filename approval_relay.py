"""Owner-only, once-only permission relay for the OpenCode V2 HTTP API.

No saved approvals: expiry rejects requests; failed replies remain retryable.
Only explicitly registered bridge sessions are ever inspected or answered.
"""
import secrets
import threading
import time
from urllib.parse import quote


class ApprovalRelay:
    def __init__(self, api, notify, owners, ttl=300, clock=time.monotonic):
        self.api = api
        self.notify = notify
        self.owners = {int(owner) for owner in owners}
        self.ttl = ttl
        self.clock = clock
        self.routes = {}
        self.pending = {}
        self.lock = threading.RLock()

    def register(self, sid, bot, chat_id):
        if sid:
            with self.lock:
                self.routes[sid] = (bot, int(chat_id))

    def owns(self, sid, token, chat_id):
        with self.lock:
            route = self.routes.get(sid)
            return bool(route and route[0]["token"] == token and route[1] == int(chat_id))

    @staticmethod
    def path(sid, rid=None):
        path = '/api/session/' + quote(sid, safe='') + '/permission'
        return path if rid is None else path + '/' + quote(rid, safe='') + '/reply'

    def poll(self):
        with self.lock:
            routes = list(self.routes.items())
        for sid, (bot, chat_id) in routes:
            result = self.api(self.path(sid), 'GET', None)
            if result.get('error') or not isinstance(result.get('data'), list):
                continue  # transport failures never approve anything
            requests = result['data']
            with self.lock:
                ids = {r.get('id') for r in requests}
                for key, entry in list(self.pending.items()):
                    if entry['sid'] == sid and entry['rid'] not in ids:
                        del self.pending[key]
                known = {(e['sid'], e['rid']) for e in self.pending.values()}
                for request in requests:
                    rid = request.get('id')
                    if (not rid or request.get('sessionID') != sid
                            or (sid, rid) in known):
                        continue
                    key = secrets.token_hex(12)
                    self.pending[key] = {
                        'sid': sid, 'rid': rid, 'bot': bot, 'chat_id': chat_id,
                        'request': request, 'deadline': self.clock() + self.ttl,
                        'notified': set(),
                    }
                for key, entry in list(self.pending.items()):
                    if entry['sid'] != sid:
                        continue
                    if self.clock() >= entry['deadline']:
                        result = self.api(self.path(sid, entry['rid']), 'POST',
                                          {'decision': 'reject'})
                        if not result.get('error'):
                            del self.pending[key]
                        continue
                    for owner in self.owners - entry['notified']:
                        if self.notify(bot, owner, key, entry['request']):
                            entry['notified'].add(owner)

    def decide(self, key, decision, user_id, bot_token, chat_id):
        with self.lock:
            if int(user_id) not in self.owners or int(chat_id) != int(user_id):
                return False, 'Only the owner can approve in their private chat.'
            entry = self.pending.get(key)
            if entry is None:
                return False, 'Request expired or already answered.'
            if entry['bot']['token'] != bot_token:
                return False, 'This request belongs to another bot.'
            if decision not in ('once', 'reject'):
                return False, 'Invalid decision.'
            expired = self.clock() >= entry['deadline']
            result = self.api(self.path(entry['sid'], entry['rid']), 'POST',
                              {'decision': 'reject' if expired else decision})
            if result.get('error'):
                return False, 'OpenCode did not confirm; retry or check the session.'
            del self.pending[key]
            return True, ('Expired — rejected.' if expired else
                          'Approved once.' if decision == 'once' else 'Rejected.')
