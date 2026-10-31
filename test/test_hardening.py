"""Behavioral security regression tests against explicitly constructed components."""
import json
from pathlib import Path
import tarfile
import unittest
from unittest.mock import Mock

from test.support import make_application
from openbridge.services.conversations import session_key


class TestAuthBeforeDownload(unittest.TestCase):
    def test_authorization_precedes_attachment_download_and_saved_work(self):
        app = make_application(self)
        components = app.components
        bot = components.configuration.data["bots"][0]
        components.gateway.media.receive = Mock()
        update = {"update_id": 1, "message": {"message_id": 1, "from": {"id": 999},
                  "chat": {"id": 42, "type": "private"}, "voice": {"file_id": "private-file"}}}
        components.gateway.accept(bot, update)
        components.gateway.execute(bot, update, "testbot")
        components.gateway.media.receive.assert_not_called()
        self.assertEqual(components.jobs.pending(), [])
        self.assertEqual(components.jobs.db.execute("SELECT payload FROM runtime_inbox").fetchone()[0], "{}")


class TestSecretFilePermissions(unittest.TestCase):
    def test_credential_files_get_0600(self):
        app = make_application(self)
        home = app.components.configuration.paths.home
        for name in ("bot.token", "pool.json", "provision.json"):
            (home / name).write_text("secret")
            (home / name).chmod(0o644)
        app.components.maintenance.secure_credentials()
        for name in ("bot.token", "pool.json", "provision.json"):
            self.assertEqual((home / name).stat().st_mode & 0o777, 0o600)


class TestResetDeletesSessions(unittest.TestCase):
    def test_reset_uses_the_full_session_key(self):
        app = make_application(self)
        components = app.components
        bot = components.configuration.data["bots"][0]
        persona = {"nama": "Una", "persona": "helpful"}
        key = session_key(bot, 42, persona)
        components.conversations.sessions[key] = "old-session"
        components.commands.send = Mock()
        components.conversations.get = Mock(return_value="new-session")
        components.commands.handle(bot, 42, "reset")
        components.backend.delete_session.assert_called_once_with("old-session")
        self.assertNotIn(key, components.conversations.sessions)
        self.assertIn("memory wiped", components.commands.send.call_args.args[2])

    def test_failed_delete_does_not_drop_session_or_claim_memory_wiped(self):
        app = make_application(self)
        components = app.components
        bot = components.configuration.data["bots"][0]
        key = session_key(bot, 42, {"nama": "Una"})
        components.conversations.sessions[key] = "old-session"
        components.backend.delete_session.side_effect = RuntimeError("backend unavailable")
        components.commands.send = Mock()
        with self.assertRaises(RuntimeError):
            components.commands.handle(bot, 42, "reset")
        self.assertEqual(components.conversations.sessions[key], "old-session")
        components.commands.send.assert_not_called()

    def test_status_runs_without_persona_argument(self):
        app = make_application(self)
        components = app.components
        components.commands.send = Mock()
        components.commands.handle(components.configuration.data["bots"][0], 42, "status")
        self.assertIn("STATUS", components.commands.send.call_args.args[2])


class TestBackupExcludesTokens(unittest.TestCase):
    def test_backup_has_no_token_files_or_pool(self):
        app = make_application(self)
        home = app.components.configuration.paths.home
        (home / "pool.json").write_text('[{"token":"private"}]')
        app.components.maintenance.backup()
        archives = list((home / "backup").glob("gb-*.tar.gz"))
        self.assertEqual(len(archives), 1)
        with tarfile.open(archives[0]) as archive:
            names = archive.getnames()
        self.assertFalse(any(name.endswith(".token") for name in names))
        self.assertNotIn("pool.json", names)
        self.assertIn("config.json", names)
        self.assertEqual(archives[0].stat().st_mode & 0o777, 0o600)


class TestPermissionConfigSchema(unittest.TestCase):
    def load(self):
        return json.loads((Path(__file__).resolve().parents[1] / "opencode.jsonc").read_text())

    def test_uses_native_v2_permission_rules(self):
        data = self.load()
        self.assertNotIn("permission", data)
        self.assertIsInstance(data["permissions"], list)
        self.assertTrue(data["permissions"])
        for rule in data["permissions"]:
            self.assertTrue({"action", "resource", "effect"} <= rule.keys())
            self.assertIn(rule["effect"], ("allow", "deny", "ask"))

    def test_dangerous_rules_survive(self):
        rules = self.load()["permissions"]
        for action, resource, effect in (("shell", "rm *", "deny"), ("shell", "sudo *", "deny"),
            ("edit", "*.token", "deny"), ("read", "*.env*", "deny"),
            ("shell", "*", "ask"), ("edit", "*", "ask"), ("subagent", "*", "deny")):
            self.assertIn({"action": action, "resource": resource, "effect": effect}, rules)
