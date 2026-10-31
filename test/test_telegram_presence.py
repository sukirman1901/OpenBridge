"""Slow cosmetic Telegram calls must not block model execution or overwrite results."""
import threading
import time
import unittest
from unittest.mock import Mock

from openbridge.adapters.channels.telegram.presence import TelegramPresence


BOT = {'token': '123:test'}
JOB = {'id': 1, 'account_id': '123', 'conversation_id': '42', 'status_message_id': 99}


class TelegramPresenceTests(unittest.TestCase):
    def test_final_reply_does_not_wait_for_a_slow_typing_action(self):
        entered, release = threading.Event(), threading.Event()
        def call(*args, **options):
            entered.set()
            release.wait(2)
            return {'ok': True}
        service = self.service(call)
        try:
            service.open(dict(JOB, status_message_id=None))
            service.typing(1)
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            service.finalize(1, lambda: None)
            self.assertLess(time.monotonic() - started, .2)
        finally:
            release.set()

    def test_transient_ack_storage_failure_retries_storage_not_network_send(self):
        done = threading.Event()
        call = Mock(return_value={'ok': True, 'result': {'message_id': 99}})
        service = self.service(call)
        seen = []
        def persist(mid):
            seen.append(mid)
            if len(seen) == 1:
                raise OSError('database temporarily busy')
            done.set()
        service.acknowledge(1, BOT, 42, 'Queued', persist)
        self.assertTrue(done.wait(2))
        self.assertEqual(seen, [99, 99])
        self.assertEqual(call.call_count, 1)

    def service(self, call, **options):
        service = TelegramPresence(call, lambda account: BOT, Mock(), **options)
        service.start()
        self.addCleanup(service.close)
        return service

    def test_slow_status_is_off_thread_and_updates_are_coalesced(self):
        entered, release = threading.Event(), threading.Event()
        def call(*args, **kwargs):
            entered.set()
            release.wait(2)
            return {'ok': True}
        service = self.service(call)
        try:
            service.open(JOB)
            start = time.monotonic()
            for _ in range(1000):
                service.update(1, 'sid', 'working')
            self.assertLess(time.monotonic() - start, .2)
            self.assertTrue(entered.wait(1))
            self.assertEqual(service.size, 1)
        finally:
            release.set()

    def test_final_response_cannot_be_overwritten_by_stale_progress(self):
        entered, release = threading.Event(), threading.Event()
        writes = []
        def call(token, method, payload, **options):
            entered.set()
            release.wait(2)
            writes.append(payload.get('text', 'typing'))
            return {'ok': True}
        service = self.service(call)
        try:
            service.open(JOB)
            service.update(1, 'sid', 'working')
            self.assertTrue(entered.wait(1))
            service.finish(1)
            final = threading.Thread(target=lambda: service.finalize(1, lambda: writes.append('FINAL')))
            final.start()
            release.set()
            final.join(2)
            service.update(1, 'sid', 'waiting_approval')
            self.assertEqual(writes[-1], 'FINAL')
            self.assertFalse(final.is_alive())
        finally:
            release.set()

    def test_acknowledgement_does_not_hold_the_poller_and_is_once_only(self):
        entered, release, recorded = threading.Event(), threading.Event(), threading.Event()
        def call(*args, **options):
            entered.set()
            release.wait(2)
            return {'ok': True, 'result': {'message_id': 99}}
        service = self.service(call)
        seen = []
        def ack(mid):
            seen.append(mid)
            recorded.set()
        try:
            start = time.monotonic()
            service.acknowledge(1, BOT, 42, 'Queued', ack)
            self.assertLess(time.monotonic() - start, .1)
            self.assertTrue(entered.wait(1))
            release.set()
            self.assertTrue(recorded.wait(1))
            self.assertEqual(seen, [99])
        finally:
            release.set()

    def test_bounded_cosmetic_queue_sheds_load_without_losing_job_ack(self):
        service = TelegramPresence(Mock(), lambda account: BOT, Mock(), capacity=4)
        self.addCleanup(service.close)
        for identity in range(4):
            service.acknowledge(identity, BOT, 42, 'Queued', Mock())
        overflow = Mock()
        service.acknowledge(99, BOT, 42, 'Queued', overflow)
        overflow.assert_called_once_with(None)
        self.assertEqual(service.size, 4)

    def test_failed_ack_still_releases_durable_job(self):
        ready = threading.Event()
        service = self.service(Mock(side_effect=OSError('offline')))
        seen = []
        service.acknowledge(1, BOT, 42, 'Queued', lambda mid: (seen.append(mid), ready.set()))
        self.assertTrue(ready.wait(1))
        self.assertEqual(seen, [None])
