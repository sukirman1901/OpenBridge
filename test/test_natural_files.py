"""Ordinary chat can create and deliver artifacts without slash commands."""
import unittest
from unittest.mock import Mock

from test.support import make_application
from openbridge.services.conversations import session_key


class NaturalFiles(unittest.TestCase):
    def test_fresh_session_does_not_race_a_queued_compaction(self):
        app = make_application(self)
        p = app.components
        bot = p.configuration.data['bots'][0]
        persona = {'nama': bot['nama'], 'persona': bot.get('persona', '')}
        p.conversations.sessions[session_key(bot, 42, persona)] = 'old-session'
        p.conversations.fresh(bot, 42, persona)
        p.backend.compact_session.assert_not_called()
        p.backend.rename_session.assert_called_once()

    def test_plain_request_emits_durable_file_and_refreshes_capabilities(self):
        app = make_application(self)
        p = app.components
        bot = p.configuration.data['bots'][0]
        p.conversations.get = Mock(return_value='existing-session')
        p.backend.run_prompt.return_value = 'Sudah dibuat.\n@file hasil/planner.xlsx'
        p.gateway.accept(bot, {'update_id': 1, 'message': {
            'message_id': 1, 'chat': {'id': 42, 'type': 'private'},
            'from': {'id': 42, 'first_name': 'Owner'},
            'text': 'Buatkan aku Excel vision planner 2027 lalu kirim ke sini'}})
        p.runtime.run(1)
        import json
        outputs = json.loads(p.jobs.get(1)['result'])
        self.assertTrue(any(o['operation'] == 'document' and
                            o['path'] == 'hasil/planner.xlsx' for o in outputs))
        self.assertFalse(any('@file' in o.get('text', '') for o in outputs))
        prompt = p.backend.run_prompt.call_args.args[1]
        self.assertIn('@file hasil/', prompt)
        self.assertIn('No slash command is required', prompt)
        # Tool approvals alone can wait up to five minutes; a two-minute
        # interactive timeout falsely interrupts otherwise healthy work.
        self.assertGreaterEqual(p.backend.run_prompt.call_args.kwargs['timeout'], 900)

    def test_tomorrow_schedule_in_ordinary_chat_is_once_not_daily(self):
        app = make_application(self)
        p = app.components
        result = p.schedules.interpret('123', 42,
            'buatkan jadwal untuk saya besok jam 9', True)
        self.assertIsNotNone(result)
        self.assertEqual(p.store.list_schedules()[0]['kind'], 'once')
