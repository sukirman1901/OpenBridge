import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from test.support import make_application
from openbridge.adapters.persistence.sqlite.jobs import JobStore
from openbridge.services.sessions import ConversationSessions


class LatencyGuards(unittest.TestCase):
    def test_blocked_cosmetic_network_does_not_delay_the_model(self):
        cosmetic, release, accepted, model = [threading.Event() for _ in range(4)]
        def call(token, method, payload=None, **options):
            if method != 'sendMessage':
                cosmetic.set()
                release.wait(3)
            return {'ok': True, 'result': {'message_id': 99}}
        app = make_application(self, call=call, async_presence=True)
        p = app.components
        p.conversations.get = Mock(return_value='sid')
        original = p.jobs.acknowledge
        def ack(*args):
            original(*args)
            accepted.set()
        p.jobs.acknowledge = ack
        def prompt(sid, text, **options):
            options['on_accepted']('u1')
            options['on_state']('working')
            self.assertTrue(cosmetic.wait(1))
            model.set()
            return 'answer'
        p.backend.run_prompt.side_effect = prompt
        worker = None
        try:
            p.gateway.accept(p.configuration.data['bots'][0], {'update_id': 1, 'message': {
                'message_id': 1, 'from': {'id': 42}, 'chat': {'id': 42, 'type': 'private'}, 'text': 'hi'}})
            self.assertTrue(accepted.wait(1))
            worker = threading.Thread(target=p.runtime.run, args=(1,))
            worker.start()
            self.assertTrue(model.wait(1.5), 'Model was blocked behind cosmetic Telegram work')
        finally:
            release.set()
            if worker:
                worker.join(3)

    def test_prepared_briefing_survives_cache_and_process_recreation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'jobs.db')
            store = JobStore(path)
            prompt = Mock(return_value='ready')
            try:
                def service():
                    return ConversationSessions({}, threading.Lock(), lambda title: 'sid', prompt,
                        Mock(), Mock(), Mock(), prepared=store.session_prepared, mark_prepared=store.prepare_session)
                self.assertEqual(service().get('key', 'title', lambda: 'briefing-v1'), 'sid')
            finally:
                store.close()
            store = JobStore(path)
            try:
                self.assertEqual(service().get('key', 'title', lambda: 'briefing-v1'), 'sid')
                self.assertEqual(prompt.call_count, 1)
                service().get('key', 'title', lambda: 'briefing-v2')
                self.assertEqual(prompt.call_count, 2)
            finally:
                store.close()

    def test_failed_briefing_is_not_cached_as_prepared(self):
        cache = {}
        prompt = Mock(side_effect=RuntimeError('backend'))
        service = ConversationSessions(cache, threading.Lock(), lambda title: 'sid', prompt, Mock(), Mock(), Mock())
        with self.assertRaises(RuntimeError):
            service.get('key', 'title', lambda: 'briefing')
        self.assertNotIn('key', cache)

    def test_accounting_failure_does_not_fail_a_successful_answer(self):
        app = make_application(self)
        p = app.components
        p.conversations.get = Mock(return_value='sid')
        p.backend.run_prompt.return_value = 'answer'
        p.backend.session_info.side_effect = RuntimeError('metrics unavailable')
        p.gateway.accept(p.configuration.data['bots'][0], {'update_id': 1, 'message': {
            'message_id': 1, 'from': {'id': 42}, 'chat': {'id': 42, 'type': 'private'}, 'text': 'hi'}})
        p.runtime.run(1)
        self.assertEqual(p.jobs.get(1)['status'], 'succeeded')
