"""Routine engine: natural-language directives, per-item management, and the store ops."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from openbridge.services.routines import (format_expr, parse_creation,
                                          parse_directive)
from openbridge.services.schedules import Schedules
from store import Store

FIXED = datetime(2026, 10, 31, 5, 50)


def make_service():
    return Schedules(Path(tempfile.mkdtemp()), Store(), {}, lambda: [], lambda *lines: None)


class RoutinesParsing(unittest.TestCase):
    """Directive words never touch the store; every phrase stays parseable."""

    def test_parse_directive_covers_valid_ops(self):
        self.assertEqual(parse_directive('batalkan jadwal 3')['op'], 'delete')
        self.assertEqual(parse_directive('hapus routine #12')['op'], 'delete')
        self.assertEqual(parse_directive('jeda jadwal 2')['op'], 'pause')
        self.assertEqual(parse_directive('pause reminder 7')['op'], 'pause')
        self.assertEqual(parse_directive('lanjutkan jadwal 5')['op'], 'resume')
        self.assertEqual(parse_directive('resume alarm 9')['op'], 'resume')

    def test_cancel_requires_id_or_explicit_all(self):
        self.assertEqual(parse_directive('batalkan semua jadwal')['op'], 'cancel_all')
        self.assertEqual(parse_directive('cancel all routines')['op'], 'cancel_all')
        self.assertEqual(parse_directive('hapus semua jadwal')['op'], 'cancel_all')
        self.assertEqual(parse_directive('batalkan jadwal')['op'], 'ambiguous_cancel')
        self.assertEqual(parse_directive('cancel routine')['op'], 'ambiguous_cancel')

    def test_edit_directives_carry_numbers(self):
        edit = parse_directive('ubah jadwal 3 jadi jam 6')
        self.assertEqual((edit['id'], edit['hour'], edit['minute']), (3, '6', '00'))
        edit = parse_directive('edit routine 2 to 07:30')
        self.assertEqual((edit['id'], edit['hour'], edit['minute']), (2, '07', '30'))
        edit = parse_directive('jadwal 5 ke besok jam 7')
        self.assertEqual((edit['id'], edit['offset']), (5, 1))

    def test_unrelated_text_is_ignored(self):
        for text in ('halo', 'persona 3', '', None, 'siapa kamu'):
            self.assertIsNone(parse_directive(text))

    def test_once_reminder(self):
        spec = parse_creation('ingetin aku besok jam 5', FIXED)
        self.assertEqual(spec['kind'], 'once')
        self.assertEqual(spec['expr'], '2026-11-01 05:00')
        self.assertEqual(spec['label'], 'tomorrow')

    def test_reminder_in_the_past_moves_to_next_day(self):
        spec = parse_creation('ingetin aku jam 4', FIXED)
        self.assertEqual(spec['kind'], 'once')
        self.assertEqual(spec['expr'], '2026-11-01 04:00')
        self.assertEqual(spec['label'], 'tomorrow')

    def test_daily_and_weekly_routines(self):
        daily = parse_creation('jadwalkan konten jam 8', FIXED)
        self.assertEqual((daily['kind'], daily['expr']), ('cron', '08:00'))
        weekly = parse_creation('tiap senin jam 8', FIXED)
        self.assertEqual((weekly['kind'], weekly['expr']), ('cron', '00 08 * * 1'))
        english = parse_creation('every friday 09:15', FIXED)
        self.assertEqual(english['expr'], '00 09 * * 5')

    def test_creation_ignores_other_text(self):
        for text in ('halo dunia', 'siapa kamu', ''):
            self.assertIsNone(parse_creation(text, FIXED))

    def test_format_expr_renders_kinds(self):
        self.assertEqual(format_expr({'kind': 'cron', 'expr': '08:00'}), '08:00')
        self.assertEqual(format_expr({'kind': 'cron', 'expr': '00 08 * * 3'}), 'Wednesday 08:00')
        self.assertEqual(format_expr({'kind': 'once', 'expr': '2026-11-01 05:00'}), '2026-11-01 05:00')
        self.assertEqual(format_expr({'kind': 'interval', 'expr': '10m'}), '10m')


class RoutineStoreOps(unittest.TestCase):
    """get/set_active/update round-trip against the durable schedules table."""

    def setUp(self):
        self.store = Store()
        self.sid = self.store.add_schedule('botA', 42, '08:00', 'cron', 'greeting')
        self.other = self.store.add_schedule('botA', 42, '09:30', 'cron', 'news')

    def test_get_schedule(self):
        self.assertEqual(self.store.get_schedule(self.sid)['expr'], '08:00')
        self.assertIsNone(self.store.get_schedule(9999))

    def test_pause_resumes_without_deleting(self):
        self.assertTrue(self.store.set_schedule_active(self.sid, False))
        self.assertEqual([row['id'] for row in self.store.list_schedules(active_only=True)],
                         [self.other])
        self.assertTrue(self.store.set_schedule_active(self.sid, True))
        self.assertEqual([row['id'] for row in self.store.list_schedules(active_only=True)],
                         [self.sid, self.other])

    def test_pause_missing_id_is_false(self):
        self.assertFalse(self.store.set_schedule_active(9999, False))

    def test_update_preserves_kind_and_changes_expression(self):
        self.assertTrue(self.store.update_schedule_expr(self.sid, '07:15'))
        row = self.store.get_schedule(self.sid)
        self.assertEqual((row['expr'], row['kind'], row['prompt']), ('07:15', 'cron', 'greeting'))

    def test_update_missing_id_is_false(self):
        self.assertFalse(self.store.update_schedule_expr(9999, '07:15'))

    def test_update_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            self.store.update_schedule_expr(self.sid, '08:00', kind='weekly')


class RoutinesService(unittest.TestCase):
    """Full interpret flow: creation, per-item ops, ownership, ambiguity."""

    def setUp(self):
        self.schedules = make_service()
        self.store = self.schedules.store

    def interpret(self, text, account='botA', chat='42', addressed=True):
        return self.schedules.interpret(account, chat, text, addressed)

    def ids(self, account='botA'):
        return [row['id'] for row in self.store.list_schedules(bot_id=account)]

    def test_creation_confirms_and_persists(self):
        response = self.interpret('ingetin aku besok jam 5')
        self.assertIn('✅', response)
        self.assertIn('#1', response)
        self.assertEqual(self.ids(), [1])

    def test_creation_requires_addressed_message(self):
        self.assertIsNone(self.interpret('ingetin aku besok jam 5', addressed=False))
        self.assertEqual(self.ids(), [])

    def test_pause_hides_from_dispatch_but_keeps_row(self):
        self.interpret('jadwalkan konten jam 8')
        paused = self.interpret('jeda jadwal 1')
        self.assertIn('⏸', paused)
        row = self.store.get_schedule(1)  # row survives with active=0
        self.assertFalse(row['active'])
        self.assertEqual([row['id'] for row in self.schedules.list()], [])
        self.assertIn('▶️', self.interpret('lanjutkan jadwal 1'))
        self.assertEqual([row['id'] for row in self.schedules.list()], [1])

    def test_edit_updates_one_shot_time(self):
        self.interpret('ingetin aku besok jam 5')
        self.assertIn('✏️', self.interpret('ubah jadwal 1 jadi jam 9:30'))
        row = self.store.get_schedule(1)
        self.assertEqual(row['kind'], 'once')
        self.assertTrue(row['expr'].endswith('09:30'))

    def test_edit_updates_daily_clock(self):
        self.interpret('jadwalkan konten jam 8')
        self.assertIn('✏️', self.interpret('edit jadwal 1 ke 07:15'))
        self.assertEqual(self.store.get_schedule(1)['expr'], '07:15')

    def test_ambiguous_cancel_deletes_nothing(self):
        self.interpret('jadwalkan konten jam 8')
        response = self.interpret('batalkan jadwal')
        self.assertIn('Which routine?', response)
        self.assertEqual(self.ids(), [1])

    def test_single_cancel_removes_only_named_routine(self):
        self.interpret('ingetin aku besok jam 5')
        self.interpret('jadwalkan konten jam 8')
        self.assertIn('🗑️', self.interpret('batalkan jadwal 1'))
        self.assertEqual(self.ids(), [2])

    def test_cancel_all_is_explicit_wide_scope(self):
        self.interpret('ingetin aku besok jam 5')
        self.interpret('jadwalkan konten jam 8')
        response = self.interpret('batalkan semua jadwal')
        self.assertIn('All 2 routine(s)', response)
        self.assertEqual(self.ids(), [])

    def test_ops_are_scoped_to_account_and_chat(self):
        self.interpret('jadwalkan konten jam 8')
        response = self.interpret('hapus jadwal 1', account='botZ', chat='42')
        self.assertIn('No routine #1', response)
        response = self.interpret('hapus jadwal 1', account='botA', chat='999')
        self.assertIn('No routine #1', response)
        self.assertEqual(self.ids(), [1])

    def test_delete_removes_named_row(self):
        self.interpret('jadwalkan konten jam 8')
        self.interpret('tiap senin jam 8')
        self.assertIn('🗑️', self.interpret('hapus jadwal 1'))
        self.assertEqual(self.ids(), [2])


if __name__ == '__main__':
    unittest.main()
