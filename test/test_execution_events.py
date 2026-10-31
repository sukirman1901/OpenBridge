import json
import threading
import unittest
from io import BytesIO
from unittest.mock import Mock

from openbridge.adapters.execution.opencode.events import SessionEvents


class ExecutionEvents(unittest.TestCase):
    def test_only_registered_sessions_are_woken_by_events(self):
        events = SessionEvents('http://local', ('u', 'p'), Mock())
        events.watch('ours')
        events.feed({'type': 'session.execution.succeeded', 'data': {'sessionID': 'other'}})
        self.assertEqual(events.stage('ours'), 'working')
        events.feed({'type': 'permission.asked', 'data': {'sessionID': 'ours'}})
        self.assertEqual(events.stage('ours'), 'waiting_approval')
        self.assertTrue(events.wait('ours', 0))
        events.feed({'type': 'permission.replied', 'data': {'sessionID': 'ours'}})
        self.assertEqual(events.stage('ours'), 'working')
        events.unwatch('ours')
        self.assertIsNone(events.stage('ours'))

    def test_sse_decodes_json_encoded_event_and_ignores_malformed_data(self):
        stopped = threading.Event()
        data = json.dumps(json.dumps({'type': 'session.text.delta',
                                     'data': {'sessionID': 'ours', 'delta': 'private'}}))
        response = BytesIO(('data: broken\n\ndata: ' + data + '\n\n').encode())
        events = SessionEvents('http://local', ('u', 'p'), Mock())
        events.watch('ours')
        events.consume(response, stopped)
        self.assertTrue(events.wait('ours', 0))
        self.assertNotIn('private', repr(events.__dict__))

    def test_disconnect_reconnect_does_not_post_any_prompt(self):
        stopped = threading.Event()
        opener = Mock(side_effect=OSError('disconnect'))
        events = SessionEvents('http://local', ('u', 'p'), Mock(), opener=opener)
        def stop_after_retry(delay):
            stopped.set()
            return True
        stopped.wait = stop_after_retry
        events.run(stopped)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(opener.call_args.args[0].get_method(), 'GET')
