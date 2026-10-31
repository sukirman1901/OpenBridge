import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test.support import make_application
from openbridge.bootstrap import create_application
from openbridge.config import Configuration, ConfigurationError, Paths, WorkerSettings
from openbridge.infrastructure.files import safe_name, write_json
from openbridge.services.conversations import session_key


class TestExplicitBootstrap(unittest.TestCase):
    def test_import_every_package_module_and_entrypoint_without_opening_state(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                       OPENBRIDGE_HOME=directory, OPENBRIDGE_SERVICE_JSON=directory + "/missing")
            code = """
import sqlite3, threading, urllib.request, pkgutil
def forbidden(*args, **kwargs):
    raise AssertionError('Import attempted runtime I/O')
sqlite3.connect = forbidden
threading.Thread.start = forbidden
urllib.request.urlopen = forbidden
import bridge, openbridge
for module in pkgutil.walk_packages(openbridge.__path__, openbridge.__name__ + '.'):
    __import__(module.name)
assert callable(bridge.main)
"""
            result = subprocess.run([sys.executable, "-c", code], cwd=directory, env=env, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_factory_does_not_start_threads_or_contact_providers(self):
        with patch("threading.Thread.start", side_effect=AssertionError("factory started thread")):
            app = make_application(self)
        app.components.backend.run_prompt.assert_not_called()
        self.assertFalse(app.runtime.threads)
        self.assertFalse(app.pollers)

    def test_real_factory_lifecycle_starts_stops_and_closes_without_provider_work(self):
        def call(token, method, payload=None, timeout=45):
            if method == "getMe":
                return {"ok": True, "result": {"id": 123, "username": "testbot"}}
            if method == "getUpdates":
                app.shutdown.set()
                return {"ok": True, "result": []}
            return {"ok": True}
        app = make_application(self, call=call)
        app.run()
        self.assertTrue(app.closed)
        self.assertTrue(app.shutdown.is_set())
        app.components.backend.run_prompt.assert_not_called()
        self.assertFalse(any(thread.is_alive() for thread in app.loop_threads))

    def test_two_instances_have_independent_state_and_routes(self):
        first, second = make_application(self), make_application(self)
        self.assertIsNot(first.runtime, second.runtime)
        self.assertIsNot(first.components.jobs.db, second.components.jobs.db)
        self.assertIsNot(first.components.conversations.sessions, second.components.conversations.sessions)
        self.assertIsNot(first.components.commands.cache, second.components.commands.cache)
        self.assertIsNot(first.components.approvals.routes, second.components.approvals.routes)

    def test_stale_account_metadata_cannot_route_a_new_token_as_the_old_bot(self):
        app = make_application(self, changes={"bots": [{"nama": "Una", "token": "{file:bot.token}", "bot_id": "999"}]})
        bot = app.components.configuration.data["bots"][0]
        self.assertEqual(bot["bot_id"], "123")
        self.assertEqual(app.account_key(bot), ("telegram", "123"))

    def test_invalid_owner_is_rejected_before_database_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for owners in ([], [True], ["invalid"]):
                config = Configuration(Paths(home, home / "missing"), {"owner_ids": owners}, WorkerSettings())
                with self.assertRaises(ConfigurationError):
                    create_application(config, backend=Mock(), channel_call=Mock())
            self.assertFalse((home / "groupbot.db").exists())

    def test_explicit_close_releases_owned_sqlite_connections(self):
        app = make_application(self)
        app.close()
        with self.assertRaises(Exception):
            app.components.jobs.db.execute("SELECT 1")
        self.assertTrue(app.closed)
        app.close()  # Idempotent; no second context/resource mutation.

    def test_factory_failure_closes_previously_opened_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Configuration(Paths(Path(directory), Path(directory) / "missing"), {"owner_ids": [42]}, WorkerSettings())
            store = Mock()
            with patch("openbridge.bootstrap.Store", return_value=store), \
                 patch("openbridge.bootstrap.TaskQueue", side_effect=RuntimeError("injected failure")):
                with self.assertRaises(RuntimeError):
                    create_application(config, backend=Mock(), channel_call=Mock())
            store.close.assert_called_once()


class TestPersonaAndScheduleBoundaries(unittest.TestCase):
    def test_invalid_persona_file_never_deletes_existing_personas(self):
        app = make_application(self)
        service = app.components.personas
        service.store.upsert_persona("Una", "keep")
        for content in ("{broken", '[{"nama":"Una"}]', '{"unexpected":"shape"}'):
            service.path.write_text(content)
            service.sync()
            self.assertEqual(service.store.list_personas()[0]["persona"], "keep")

    def test_persona_removal_is_archived_before_deletion(self):
        app = make_application(self)
        service = app.components.personas
        service.store.upsert_persona("Old", "private description")
        service.path.write_text("[]")
        service.sync()
        self.assertEqual(service.store.list_personas(), [])
        archive = service.home / "backup" / "personas.arsip.jsonl"
        self.assertIn("Old", archive.read_text())

    def test_unowned_schedule_import_is_retained_not_silently_discarded(self):
        app = make_application(self)
        service = app.components.schedules
        source = [{"bot_id": "456", "chat_id": 42, "jam": "12:00", "prompt": "do not discard"}]
        write_json(service.path, source)
        service.sync()
        self.assertEqual(json.loads(service.path.read_text()), source)
        self.assertEqual(service.store.list_schedules(), [])

    def test_memory_names_cannot_escape_the_state_directory(self):
        app = make_application(self)
        for name in ("../outside", "/absolute", "..", "bad\\path"):
            with self.assertRaises(ValueError):
                app.components.personas.memory(name)
        self.assertTrue(app.components.personas.memory("Una").is_file())

    def test_backup_failure_can_retry_and_does_not_publish_partial_archive(self):
        app = make_application(self)
        service = app.components.maintenance
        with patch("openbridge.services.maintenance.tarfile.open", side_effect=RuntimeError("archive failed")):
            with self.assertRaises(RuntimeError):
                service.backup()
        self.assertIsNone(service.last_backup)
        self.assertFalse(list((service.home / "backup").glob("gb-*.tar.gz")))
        service.backup()
        self.assertEqual(len(list((service.home / "backup").glob("gb-*.tar.gz"))), 1)


class TestCommandAndCallbackBoundaries(unittest.TestCase):
    def callback(self, data, user=42, chat=42):
        return {"id": "callback", "from": {"id": user}, "data": data,
                "message": {"message_id": 1, "chat": {"id": chat}}}

    def test_unknown_user_cannot_trigger_callback_actions(self):
        app = make_application(self)
        components = app.components
        components.commands.callback(components.configuration.data["bots"][0], self.callback("c-rs", user=999))
        components.backend.delete_session.assert_not_called()
        components.backend.run_prompt.assert_not_called()

    def test_stop_cannot_target_another_conversation_or_bot(self):
        app = make_application(self)
        components = app.components
        bot = components.configuration.data["bots"][0]
        components.approvals.register("private-session", bot, 43)
        components.commands.callback(bot, self.callback("st-private-session"))
        components.backend.interrupt.assert_not_called()
        components.approvals.register("private-session", bot, 42)
        components.commands.callback(bot, self.callback("st-private-session"))
        components.backend.interrupt.assert_called_once_with("private-session")

    def test_negative_or_expired_selection_does_not_change_models(self):
        app = make_application(self)
        components = app.components
        bot = components.configuration.data["bots"][0]
        components.commands.cache["123"] = {"models": [{"providerID": "p", "modelID": "m"}]}
        for data in ("md--1", "md-4", "md-no-number"):
            components.commands.callback(bot, self.callback(data))
        components.backend.set_model.assert_not_called()

    def test_model_caches_are_account_scoped(self):
        app = make_application(self)
        components = app.components
        components.backend.models.return_value = [{"providerID": "p", "modelID": "m"}]
        components.commands.panel(components.configuration.data["bots"][0], 42, "model")
        self.assertIn("123", components.commands.cache)
        self.assertNotIn("456", components.commands.cache)

    def test_help_buttons_route_to_real_commands(self):
        app = make_application(self)
        components = app.components
        components.commands.handle = Mock()
        bot = components.configuration.data["bots"][0]
        components.commands.callback(bot, self.callback("c-st"))
        components.commands.handle.assert_called_once_with(bot, 42, "status")

    def test_fresh_context_archives_old_lookup_title_before_new_session(self):
        app = make_application(self)
        service = app.components.conversations
        bot = app.components.configuration.data["bots"][0]
        persona = {"nama": "Una", "persona": "helpful"}
        service.sessions[session_key(bot, 42, persona)] = "old"
        service.get = Mock(return_value="new")
        app.components.backend.run_prompt.return_value = "handoff"
        self.assertEqual(service.fresh(bot, 42, persona), "new")
        app.components.backend.rename_session.assert_called_once()
        self.assertEqual(service.get.call_args.kwargs["handoff"], "handoff")


class TestProvisioningBoundaries(unittest.TestCase):
    def prepare(self):
        app = make_application(self)
        service = app.components.provisioning
        service.send = Mock(return_value={"ok": True})
        service.edit = Mock()
        service.start_poller = Mock()
        bot = app.components.configuration.data["bots"][0]
        return app, service, bot

    def test_provisioning_is_disabled_by_default(self):
        app, service, bot = self.prepare()
        write_json(service.home / "provision.json", [{"nama": "New"}])
        service.scan()
        service.send.assert_not_called()

    def test_owner_confirmation_is_private_bound_once_only_and_callback_is_short(self):
        app, service, bot = self.prepare()
        service.create = Mock(return_value=(True, "newbot"))
        service.offer("Very long Unicode name ΩΩΩ")
        key = next(iter(service.pending))
        buttons = service.send.call_args.kwargs["buttons"]
        self.assertLessEqual(len(buttons[0][1].encode()), 64)
        self.assertFalse(service.decide(bot, 99, 1, "y", key, 42))
        self.assertFalse(service.decide(bot, 42, 1, "y", key, 999))
        self.assertTrue(service.decide(bot, 42, 1, "y", key, 42))
        self.assertFalse(service.decide(bot, 42, 1, "y", key, 42))
        service.create.assert_called_once()

    def test_failed_identity_does_not_consume_spare_token(self):
        app, service, bot = self.prepare()
        source = [{"token": "456:private"}]
        write_json(service.home / "pool.json", source)
        service.call = Mock(return_value={"error": "connection failed"})
        self.assertFalse(service.create("New")[0])
        self.assertEqual(json.loads((service.home / "pool.json").read_text()), source)

    def test_existing_token_file_is_never_overwritten(self):
        app, service, bot = self.prepare()
        write_json(service.home / "pool.json", [{"token": "456:private"}])
        (service.home / "new.token").write_text("user-owned")
        service.call = Mock(return_value={"ok": True, "result": {"id": 456, "username": "newbot"}})
        self.assertFalse(service.create("New")[0])
        self.assertEqual((service.home / "new.token").read_text(), "user-owned")

    def test_creation_preserves_existing_token_references_and_uses_private_file(self):
        app, service, bot = self.prepare()
        new_token = "456:" + "n" * 32
        write_json(service.home / "pool.json", [{"token": new_token}])
        service.call = Mock(return_value={"ok": True, "result": {"id": 456, "username": "newbot"}})
        self.assertTrue(service.create("New")[0])
        source = json.loads((service.home / "config.json").read_text())
        self.assertEqual(source["bots"][0]["token"], "{file:bot.token}")
        self.assertEqual(source["bots"][1]["token"], "{file:new.token}")
        self.assertNotIn(new_token, (service.home / "config.json").read_text())
        self.assertEqual((service.home / "new.token").stat().st_mode & 0o777, 0o600)
        service.start_poller.assert_called_once()
        service.personas.sync()
        self.assertTrue(any(row["nama"] == "New" for row in service.personas.list()))


class TestMediaBoundary(unittest.TestCase):
    def test_attachment_size_is_checked_before_download_and_errors_do_not_echo_tokens(self):
        app = make_application(self)
        media = app.components.gateway.media
        media.opener = Mock()
        bot = app.components.configuration.data["bots"][0]
        message = {"message_id": 1, "chat": {"id": 42}, "voice": {"file_id": "file", "file_size": media.limit + 1}}
        self.assertEqual(media.receive(bot, message), ("", ""))
        media.opener.assert_not_called()

    def test_download_uses_account_and_chat_scoped_private_file(self):
        app = make_application(self)
        media = app.components.gateway.media
        media.call = Mock(return_value={"result": {"file_path": "voice/file.oga"}})
        media.opener = Mock(return_value=io.BytesIO(b"audio"))
        bot = app.components.configuration.data["bots"][0]
        description, path = media.receive(bot, {"message_id": 1, "chat": {"id": 42}, "voice": {"file_id": "file"}})
        self.assertTrue(path)
        self.assertIn("123-42-1-voice", path)
        self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
