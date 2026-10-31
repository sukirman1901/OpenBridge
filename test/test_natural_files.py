"""Ordinary chat can create and deliver artifacts without slash commands."""
import unittest
from unittest.mock import Mock

from test.support import make_application


class NaturalFiles(unittest.TestCase):
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

    def test_tomorrow_schedule_in_ordinary_chat_is_once_not_daily(self):
        app = make_application(self)
        p = app.components
        result = p.schedules.interpret('123', 42,
            'buatkan jadwal untuk saya besok jam 9', True)
        self.assertIsNotNone(result)
        self.assertEqual(p.store.list_schedules()[0]['kind'], 'once')
