import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

from openbridge.app import Application
from openbridge.config import Paths, ConfigurationError, load_config, load_registration, WorkerSettings
from openbridge.services.delivery import OutputService
from openbridge.services.sessions import ConversationSessions, BackgroundSessions
from openbridge.services.job_commands import JobCommands
from openbridge.services.briefing import conversation_briefing
from openbridge.adapters.channels.telegram.delivery import TelegramDelivery
from openbridge.adapters.channels.telegram.polling import TelegramPoller
from openbridge.adapters.execution.opencode import OpenCodeClient
from openbridge.adapters.persistence.sqlite import JobStore
from openbridge.domain.messages import ConversationKey


class TestConfiguration(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        self.paths = Paths(self.home, self.home / "service.json")

    def write(self, data):
        self.paths.config.write_text(json.dumps(data))

    def test_loading_resolves_token_without_rewriting_source(self):
        self.write({"bots": [{"nama": "Una", "token": "{file:bot.token}"}], "owner_ids": [42], "unknown": {"keep": True}})
        (self.home / "bot.token").write_text("private-credential\n")
        original = self.paths.config.read_bytes()
        result = load_config(self.paths)
        self.assertEqual(result.data["bots"][0]["token"], "private-credential")
        self.assertEqual(result.data["unknown"], {"keep": True})
        self.assertEqual(self.paths.config.read_bytes(), original)
        self.assertNotIn("private-credential", repr(result))

    def test_missing_token_is_unavailable_not_a_literal_reference(self):
        self.write({"bots": [{"token": "{file:missing.token}"}]})
        self.assertEqual(load_config(self.paths).data["bots"][0]["token"], "")

    def test_invalid_json_and_shape_do_not_echo_config_contents(self):
        for value in (["private-credential"], {"runtime": []}, {"bots": [42]}):
            self.write(value)
            with self.assertRaises(ConfigurationError) as error:
                load_config(self.paths)
            self.assertNotIn("private-credential", str(error.exception))
        self.paths.config.write_text('{"private-credential":')
        with self.assertRaises(ConfigurationError):
            load_config(self.paths)

    def test_explicit_worker_settings_and_legacy_fallback(self):
        self.assertEqual(WorkerSettings.from_config({}).interactive, 1)
        self.assertEqual(WorkerSettings.from_config({"runtime": {"conversation_workers": 3}}).interactive, 3)
        self.assertEqual(WorkerSettings.from_config({"runtime": {
            "conversation_workers": 3, "interactive_workers": 2, "background_workers": 1}}).interactive, 2)

    def test_invalid_worker_limits_fail_before_threads_start(self):
        for value in (0, -1, True, "secret-value", 1.5):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                WorkerSettings.from_config({"runtime": {"background_workers": value}})

    def test_service_registration_failure_is_sanitized(self):
        self.paths.service.write_text('{"url": "private-credential"}')
        with self.assertRaises(ConfigurationError) as error:
            load_registration(self.paths.service)
        self.assertNotIn("private-credential", str(error.exception))

    def test_path_discovery_does_not_require_existing_state(self):
        home = self.home / "not-created"
        result = Paths.discover("/unused", {"OPENBRIDGE_HOME": str(home), "OPENBRIDGE_SERVICE_JSON": "custom-service"})
        self.assertEqual(result.home, home)
        self.assertFalse(home.exists())


class TestOutputService(unittest.TestCase):
    def setUp(self):
        self.runtime = Mock()
        self.runtime.current_job.return_value = {"channel": "telegram", "account_id": "123", "conversation_id": "42"}
        self.channel = Mock(channel="telegram")
        self.service = OutputService(self.runtime, self.channel)

    def test_job_output_is_captured_not_sent(self):
        self.service.send("123", 42, "x" * 3501, [("Next", "next")])
        self.assertEqual([len(call.args[0]["text"]) for call in self.runtime.emit.call_args_list], [3500, 1])
        self.assertIsNone(self.runtime.emit.call_args_list[0].args[0]["buttons"])
        self.assertEqual(self.runtime.emit.call_args_list[1].args[0]["buttons"], [("Next", "next")])
        self.channel.send_text.assert_not_called()

    def test_cross_account_conversation_or_channel_output_is_rejected(self):
        for account, conversation in (("456", 42), ("123", 43)):
            with self.assertRaises(RuntimeError):
                self.service.send(account, conversation, "private")
        self.channel.channel = "whatsapp"
        with self.assertRaises(RuntimeError):
            self.service.send("123", 42, "private")
        self.runtime.emit.assert_not_called()

    def test_direct_control_response_uses_channel_port(self):
        self.runtime.current_job.return_value = None
        self.service.send("123", 42, "status")
        self.channel.send_text.assert_called_once_with("123", 42, "status", None, 2)

    def test_edit_is_captured_without_exposing_transport(self):
        self.service.edit("123", 42, 99, "answer", buttons=[])
        self.assertEqual(self.runtime.emit.call_args.args[0]["message_id"], 99)
        self.channel.edit_text.assert_not_called()


class TestDeliveryAdapter(unittest.TestCase):
    def adapter(self, response):
        call = Mock(return_value=response)
        adapter = TelegramDelivery(call, lambda account: {"token": "123:test"}, Mock(), Mock())
        return adapter, call

    def delivery(self, operation="send"):
        return {"id": 1, "job_id": 2, "account_id": "123", "conversation_id": "42",
                "payload": {"operation": operation, "text": "answer", "message_id": 99}}

    def test_rate_limit_keeps_server_retry_delay(self):
        adapter, _ = self.adapter({"error": 429, "body": json.dumps({"parameters": {"retry_after": 75}})})
        self.assertEqual(adapter.deliver(self.delivery()), ("pending", "Telegram rate limit", 75))

    def test_ambiguous_send_and_edit_have_different_retry_policy(self):
        adapter, _ = self.adapter({"error": "URLError"})
        self.assertEqual(adapter.deliver(self.delivery())[0], "uncertain")
        self.assertEqual(adapter.deliver(self.delivery("edit"))[0], "pending")

    def test_permanent_failure_and_unknown_operation_are_dead_letters(self):
        adapter, call = self.adapter({"error": 403})
        self.assertEqual(adapter.deliver(self.delivery())[0], "dead")
        call.reset_mock()
        self.assertEqual(adapter.deliver(self.delivery("delete"))[0], "dead")
        call.assert_not_called()

    def test_ambiguous_direct_failure_is_not_replayed_in_legacy_outbox(self):
        adapter, _ = self.adapter({"error": 503})
        adapter.send_text("123", 42, "answer")
        adapter.enqueue_legacy.assert_not_called()

    def test_known_rejected_direct_send_can_use_legacy_outbox(self):
        adapter, _ = self.adapter({'error': 429})
        adapter.send_text('123', 42, 'answer')
        adapter.enqueue_legacy.assert_called_once_with("123:test", 42, "answer")

    def test_ambiguous_parse_text_does_not_trigger_a_duplicate_send(self):
        adapter, call = self.adapter({"error": 503, "body": "upstream parse failed"})
        delivery = self.delivery()
        delivery["payload"]["parse"] = True
        self.assertEqual(adapter.deliver(delivery)[0], "uncertain")
        call.assert_called_once()


class TestPollingBoundary(unittest.TestCase):
    def test_acceptance_precedes_legacy_cursor_and_stop_does_not_drop_received_work(self):
        stopped = threading.Event()
        jobs, legacy, accept = Mock(), Mock(), Mock()
        jobs.cursor.side_effect = [0, 2]

        def call(token, method, payload=None, timeout=45):
            if method == "getMe":
                return {"ok": True, "result": {"id": 123, "username": "testbot"}}
            if method == "getUpdates":
                return {"ok": True, "result": [{"update_id": 1}]}
            return {"ok": True}

        def commit(bot, update):
            legacy.set_offset.assert_not_called()
            stopped.set()
        accept.side_effect = commit
        TelegramPoller(call, jobs, legacy, accept, stopped, Mock()).run({"token": "123:test", "nama": "Una"})
        accept.assert_called_once()
        legacy.set_offset.assert_called_once_with("123", 2)

    def test_shutdown_during_long_poll_leaves_unaccepted_updates_at_source(self):
        stopped, accept, jobs = threading.Event(), Mock(), Mock()
        def call(token, method, payload=None, timeout=45):
            if method == "getMe":
                return {"ok": True, "result": {"id": 123}}
            if method == "getUpdates":
                stopped.set()
                return {"ok": True, "result": [{"update_id": 1}]}
            return {"ok": True}
        legacy = Mock()
        TelegramPoller(call, jobs, legacy, accept, stopped, Mock()).run({"token": "123:test", "nama": "Una"})
        accept.assert_not_called()
        legacy.set_offset.assert_not_called()


class TestSessionBoundaries(unittest.TestCase):
    def test_registration_and_checkpoint_precede_first_prompt_and_cache_reuses_session(self):
        events = []
        service = ConversationSessions({}, threading.Lock(), Mock(return_value="session"),
            lambda *args: events.append("prompt") or "ready", lambda sid: events.append("register"),
            lambda sid: events.append("checkpoint"), Mock())
        self.assertEqual(service.get("key", "title", lambda: "briefing"), "session")
        self.assertEqual(events, ["register", "checkpoint", "prompt"])
        service.get("key", "title", lambda: self.fail("Cached session rebriefed"))
        self.assertEqual(events[-2:], ["register", "checkpoint"])
        service.open_session.assert_called_once()

    def test_background_resume_enforces_policy_and_never_creates_another_session(self):
        backend, register, checkpoint = Mock(), Mock(), Mock()
        service = BackgroundSessions(backend, "/work", lambda: ["rules"], register, checkpoint)
        self.assertEqual(service.get({"id": 1, "kind": "task", "session_id": "existing"}), "existing")
        backend.enforce_permissions.assert_called_once_with("existing", ["rules"])
        backend.create_session.assert_not_called()
        checkpoint.assert_called_once_with("existing")

    def test_native_idle_contract_fails_closed(self):
        for active, inbox, expected in (({}, [], True), ({"session": {}}, [], False),
                                       ({}, [{}], False), (None, [], False), ({}, None, False)):
            client = OpenCodeClient("http://local", (), Mock(side_effect=[{"data": active}, {"data": inbox}]))
            self.assertEqual(client.session_idle("session"), expected)

    def test_idle_transport_exception_cannot_release_a_fence(self):
        client = OpenCodeClient("http://local", (), Mock(side_effect=RuntimeError("unavailable")))
        self.assertFalse(client.session_idle("session"))

    def test_failed_session_listing_cannot_silently_create_a_duplicate(self):
        transport = Mock(return_value={"error": 503})
        client = OpenCodeClient("http://local", (), transport)
        with self.assertRaises(RuntimeError):
            client.open_session("existing title", "/work", [])
        self.assertEqual(transport.call_count, 1)

    def test_reused_session_enforces_permissions_without_creation(self):
        transport = Mock(side_effect=[{"data": [{"id": "existing", "title": "title"}]}, {}])
        client = OpenCodeClient("http://local", (), transport)
        self.assertEqual(client.open_session("title", "/work", ["rules"]), "existing")
        self.assertEqual(transport.call_args_list[1].args[1], "PATCH")
        self.assertEqual(transport.call_args_list[1].args[2], {"permissions": ["rules"]})

    def test_briefing_preserves_owner_scope_and_handoff(self):
        text = conversation_briefing(name="Una", persona="helpful", account_id="123", chat_id=42,
            memory_file="memory.md", schedule_file="cron.json", persona_file="personas.json",
            provision_file="provision.json", output_dir="hasil", siblings=[], handoff="previous decisions")
        self.assertIn("previous decisions", text)
        self.assertIn("Never touch schedules belonging to other bots", text)
        self.assertIn("Never ask the user to paste credentials", text)


class TestJobCommandBoundary(unittest.TestCase):
    def test_channel_scope_and_unknown_command_cannot_resolve_another_job(self):
        jobs = JobStore()
        self.addCleanup(jobs.close)
        job_id, _ = jobs.accept(ConversationKey("whatsapp", "123", "42"), 1, {})
        send, backend = Mock(), Mock()
        controller = JobCommands(jobs, backend, send, channel="telegram")
        controller.handle("123", 42, f"/resolve {job_id} confirm")
        self.assertIn("not found", send.call_args.args[0])
        backend.session_idle.assert_not_called()
        controller.handle("123", 42, f"/unknown {job_id} confirm")
        self.assertEqual(jobs.get(job_id)["status"], "queued")


class TestApplicationLifecycle(unittest.TestCase):
    def app(self, stopped, poller, **options):
        runtime, context = Mock(), Mock()
        pools = {name: Mock(max_workers=1) for name in ("interactive", "background")}
        for pool in pools.values():
            pool.shutdown.return_value = True
        values = dict(shutdown=stopped, runtime=runtime, worker_pools=pools, context=context,
            bots=lambda: [{"id": "account"}], poller=poller, account_key=lambda bot: bot["id"],
            prepare=lambda: True, background_loops={}, log=Mock(), drain_seconds=.2)
        values.update(options)
        return Application(**values)

    def test_successful_lifecycle_starts_and_drains_both_pools(self):
        stopped = threading.Event()
        original = signal.getsignal(signal.SIGTERM)
        app = self.app(stopped, lambda bot, index: stopped.set())
        app.run()
        app.runtime.start.assert_called_once()
        app.runtime.stop.assert_called_once()
        app.context.save.assert_called_once()
        for pool in app.worker_pools.values():
            self.assertEqual(pool.shutdown.call_args_list[0].kwargs["timeout"], 0)
            self.assertEqual(pool.shutdown.call_count, 2)
        self.assertIs(signal.getsignal(signal.SIGTERM), original)

    def test_invalid_preparation_never_starts_runtime(self):
        app = self.app(threading.Event(), Mock(), prepare=lambda: False)
        app.run()
        app.runtime.start.assert_not_called()
        app.poller.assert_not_called()

    def test_preparation_exception_still_cleans_up_and_restores_signal_handler(self):
        app = self.app(threading.Event(), Mock(), prepare=Mock(side_effect=ConfigurationError("invalid")))
        original = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(ConfigurationError):
            app.run()
        app.context.save.assert_called_once()
        self.assertIs(signal.getsignal(signal.SIGTERM), original)

    def test_failed_runtime_stop_does_not_skip_worker_cleanup(self):
        app = self.app(threading.Event(), Mock(), prepare=lambda: False)
        app.runtime.stop.side_effect = RuntimeError("failure")
        app.run()
        for pool in app.worker_pools.values():
            self.assertEqual(pool.shutdown.call_count, 2)
        app.context.save.assert_called_once()

    def test_provisioning_and_monitoring_share_one_account_poller(self):
        started, release = threading.Event(), threading.Event()
        def poller(bot, index):
            started.set()
            release.wait(2)
        app = self.app(threading.Event(), poller)
        try:
            first = app.start_poller({"id": "account"})
            self.assertTrue(started.wait(1))
            self.assertIs(app.start_poller({"id": "account"}), first)
        finally:
            app.shutdown.set()
            release.set()
            first.join(2)


class TestImportBoundary(unittest.TestCase):
    def test_new_modules_import_without_loading_bridge_or_creating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, OPENBRIDGE_HOME=directory, OPENBRIDGE_SERVICE_JSON=directory + "/missing")
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            result = subprocess.run([sys.executable, "-c", (
                "import sys; import bridge, openbridge.app, openbridge.config, openbridge.bootstrap; "
                "import openbridge.services.delivery, openbridge.services.sessions, openbridge.services.job_commands; "
                "import openbridge.adapters.channels.telegram.polling; "
                "assert callable(bridge.main)")], cwd=directory, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])
