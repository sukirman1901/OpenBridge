import threading
import unittest
from unittest.mock import Mock

from openbridge.adapters.channels.telegram.polling import TelegramPoller


class PollingBackoff(unittest.TestCase):
    def test_transient_identity_connection_failure_retries_without_monitor_delay(self):
        stopped = threading.Event()
        waits, identities = [], []
        stopped.wait = lambda seconds: waits.append(seconds)
        def call(token, method, payload=None, **options):
            if method == 'getMe':
                identities.append(1)
                return {'error': 'ConnectionResetError', 'not_sent': True} if len(identities) == 1 else {
                    'ok': True, 'result': {'id': 123}}
            if method == 'getUpdates':
                stopped.set()
            return {'ok': True, 'result': []}
        jobs = Mock()
        jobs.cursor.return_value = 0
        TelegramPoller(call, jobs, Mock(), Mock(), stopped, Mock()).run({'token': '123:t', 'nama': 'Una'})
        self.assertEqual(len(identities), 2)
        self.assertEqual(waits[0], 1)

    def test_rate_limited_poll_honors_retry_after(self):
        stopped = threading.Event()
        waits = []
        def wait(seconds):
            waits.append(seconds)
            stopped.set()
        stopped.wait = wait
        def call(token, method, payload=None, **options):
            if method == 'getMe':
                return {'ok': True, 'result': {'id': 123}}
            return {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 19}}
        jobs, legacy = Mock(), Mock()
        jobs.cursor.return_value = 0
        TelegramPoller(call, jobs, legacy, Mock(), stopped, Mock()).run({'token': '123:t', 'nama': 'Una'})
        self.assertEqual(waits, [19])

    def test_failed_polls_back_off_instead_of_busy_loop(self):
        stopped = threading.Event()
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                stopped.set()
        stopped.wait = wait
        def call(token, method, payload=None, **options):
            if method == 'getMe':
                return {'ok': True, 'result': {'id': 123}}
            return {'error': 'URLError'}
        jobs = Mock()
        jobs.cursor.return_value = 0
        TelegramPoller(call, jobs, Mock(), Mock(), stopped, Mock()).run({'token': '123:t', 'nama': 'Una'})
        self.assertEqual(waits, [1, 2, 4])
