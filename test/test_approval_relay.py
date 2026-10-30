import unittest
from approval_relay import ApprovalRelay


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.notices = [], []
        self.now = 0
        self.fail = False
        self.requests = [{'id': 'p1', 'sessionID': 's1', 'action': 'shell',
                          'resources': ['echo hello']}]
        self.bot = {'token': 'test-token'}
        self.relay = ApprovalRelay(self.api, self.notify, [42],
                                   clock=lambda: self.now)
        self.relay.register('s1', self.bot, -100)

    def api(self, path, method, body):
        self.calls.append((path, method, body))
        if method == 'GET':
            return {'data': self.requests}
        if self.fail:
            return {'error': 503}
        self.requests = []
        return {}

    def notify(self, bot, owner, key, request):
        self.notices.append((owner, key))
        return True

    def key(self):
        self.relay.poll()
        return self.notices[0][1]

    def test_notify_owner_once_not_group(self):
        self.key()
        self.relay.poll()
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.notices[0][0], 42)

    def test_approve_once_and_duplicate_rejected(self):
        key = self.key()
        self.assertTrue(self.relay.decide(key, 'once', 42, 'test-token', 42)[0])
        self.assertEqual(self.calls[-1][2], {'decision': 'once'})
        self.assertFalse(self.relay.decide(key, 'once', 42, 'test-token', 42)[0])

    def test_reject(self):
        self.assertTrue(self.relay.decide(self.key(), 'reject', 42, 'test-token', 42)[0])
        self.assertEqual(self.calls[-1][2], {'decision': 'reject'})

    def test_wrong_user_bot_group_and_always_rejected(self):
        key = self.key()
        for user, bot, chat, decision in [
            (99, 'test-token', 99, 'once'), (42, 'other-token', 42, 'once'),
            (42, 'test-token', -100, 'once'), (42, 'test-token', 42, 'always')]:
            self.assertFalse(self.relay.decide(key, decision, user, bot, chat)[0])
        self.assertFalse(any(method == 'POST' for _, method, _ in self.calls))

    def test_expiry_rejects(self):
        self.key()
        self.now = 301
        self.relay.poll()
        self.assertEqual(self.calls[-1][2], {'decision': 'reject'})
        self.assertFalse(self.relay.pending)

    def test_reply_failure_retains_request(self):
        key = self.key()
        self.fail = True
        self.assertFalse(self.relay.decide(key, 'once', 42, 'test-token', 42)[0])
        self.assertIn(key, self.relay.pending)

    def test_ignore_foreign_sessions(self):
        self.requests[0]['sessionID'] = 'foreign'
        self.relay.poll()
        self.assertFalse(self.notices)

    def test_stale_request_removed(self):
        key = self.key()
        self.requests = []
        self.relay.poll()
        self.assertNotIn(key, self.relay.pending)
