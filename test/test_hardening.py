"""Regression tests for the security & correctness hardening passes.

These follow the repo's "verify the real artifact" style: some tests inspect the
bridge source because the ordering of checks inside the polling loop is the
behavior under test (attachments must never be downloaded before authorization).
"""
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _offline_service_file():
    """bridge.py resolves the OpenCode server from the user's service.json at
    import time; CI has none. Point OPENBRIDGE_SERVICE_JSON at an offline dummy
    so the module can be imported without a live OpenCode install."""
    real = os.path.expanduser("~/.local/state/opencode/service.json")
    if "OPENBRIDGE_SERVICE_JSON" in os.environ or os.path.exists(real):
        return
    d = tempfile.mkdtemp(prefix="openbridge-test-")
    p = os.path.join(d, "service.json")
    with open(p, "w") as f:
        json.dump({"url": "http://127.0.0.1:1", "password": "x"}, f)
    os.environ["OPENBRIDGE_SERVICE_JSON"] = p


_offline_service_file()
import bridge  # noqa: E402


class TestAuthBeforeDownload(unittest.TestCase):
    """Strangers must not be able to fill the disk with 20 MB uploads."""

    def test_authorization_precedes_attachment_download(self):
        src = open(os.path.join(os.path.dirname(bridge.__file__), "bridge.py")).read()
        auth_at = src.find('if not chat_id or not authorized(cfg, msg.get("from") or {}):\n                continue')
        att_at = src.find("lamp_desc, _local = ambil_lampiran(bot, msg)")
        self.assertNotEqual(auth_at, -1, "authorization check vanished from the poll loop")
        self.assertNotEqual(att_at, -1, "attachment intake vanished from the poll loop")
        self.assertLess(auth_at, att_at, "authorization must run before any download")


class TestSecretFilePermissions(unittest.TestCase):
    def test_credential_files_get_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_here = bridge.HERE
            old_cfg = bridge.cfg
            try:
                bridge.HERE = tmp
                bridge.cfg = {"bots": [{"nama": "Una", "token": {"file": "tok.token"}}]}
                for name in ("tok.token", "pool.json", "provision.json"):
                    with open(os.path.join(tmp, name), "w") as f:
                        f.write("secret")
                    os.chmod(os.path.join(tmp, name), 0o644)
                bridge.amankan_berkas_rahasia()
                for name in ("tok.token", "pool.json", "provision.json"):
                    mode = os.stat(os.path.join(tmp, name)).st_mode & 0o777
                    self.assertEqual(mode, 0o600, f"{name} must be owner-only")
            finally:
                bridge.HERE = old_here
                bridge.cfg = old_cfg


class TestResetDeletesSessions(unittest.TestCase):
    def _call(self, cmd, arg=""):
        sent, deleted, created = [], [], []
        old = (bridge.http, bridge.sesi_untuk, bridge.send_msg, bridge.sessions)
        bridge.sessions = {("Una", 42, "grup"): "ses_old"}
        try:
            bridge.http = lambda url, method="GET", body=None, auth=None, timeout=45: (
                sent.append((method, url)), {})[1]
            bridge.sesi_untuk = lambda bot, chat_id, persona, mode="grup", handoff="": (
                created.append(chat_id), "ses_new")[1]
            bridge.send_msg = lambda token, chat_id, text, buttons=None, cols=2: sent and None
            captured = []
            bridge.send_msg = lambda token, chat_id, text, buttons=None, cols=2: captured.append(text)
            bridge.tangani_perintah({"token": "x", "nama": "Una"}, 42, cmd, arg, persona=None)
            return captured, sent, created
        finally:
            bridge.http, bridge.sesi_untuk, bridge.send_msg, bridge.sessions = old

    def test_reset_uses_the_full_session_key(self):
        captured, sent, created = self._call("reset")
        self.assertEqual(sent, [("DELETE", f"{bridge.API_URL}/api/session/ses_old")],
                         "/reset must delete the session stored under the real 3-part key")
        self.assertEqual(created, [42])
        self.assertTrue(any("memory wiped" in t for t in captured))

    def test_status_runs_without_a_persona_argument(self):
        captured, sent, created = self._call("status")
        self.assertTrue(any("STATUS" in t for t in captured),
                        "/status must render with the default persona, not crash")


class TestBackupExcludesTokens(unittest.TestCase):
    def test_backup_tarball_has_no_token_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (bridge.HERE, bridge.MEMORI_DIR, bridge.BEKUP)
            try:
                bridge.HERE = tmp
                bridge.MEMORI_DIR = os.path.join(tmp, "memori")
                bridge.BEKUP = {}
                for name in ("config.json", "unabot.token", "pool.json"):
                    with open(os.path.join(tmp, name), "w") as f:
                        f.write("{}")
                sqlite3.connect(os.path.join(tmp, "groupbot.db")).close()
                bridge.backup_harian()
                archive = os.path.join(tmp, "backup",
                                       f"gb-{bridge.datetime.now(bridge.WIB).strftime('%Y-%m-%d')}.tar.gz")
                self.assertTrue(os.path.exists(archive), "backup archive not produced")
                with tarfile.open(archive) as tgz:
                    names = tgz.getnames()
                self.assertFalse(any(n.endswith(".token") for n in names),
                                 "credential tokens must never be archived")
                self.assertNotIn("pool.json", names,
                                 "pool.json may hold spare tokens — never archive it")
                self.assertIn("config.json", names)
            finally:
                bridge.HERE, bridge.MEMORI_DIR, bridge.BEKUP = old


class TestPermissionConfigSchema(unittest.TestCase):
    """OpenCode reads a `permission` OBJECT keyed by tool name; an unknown
    `permissions` ARRAY is silently ignored, which would leave every deny rule
    dead. Guard the schema so it can't regress unnoticed."""

    def _load(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "opencode.jsonc")) as f:
            return json.load(f)

    def test_uses_singular_permission_object(self):
        data = self._load()
        self.assertNotIn("permissions", data,
                         "the array form is not part of the OpenCode schema — remove it")
        self.assertIn("permission", data)
        self.assertIsInstance(data["permission"], dict)

    def test_dangerous_rules_survive(self):
        rules = self._load()["permission"]
        self.assertEqual(rules["bash"].get("rm *"), "deny")
        self.assertEqual(rules["bash"].get("sudo *"), "deny")
        self.assertEqual(rules["edit"].get("**.token"), "deny")
        self.assertEqual(rules["read"].get("*.env"), "deny")


if __name__ == "__main__":
    unittest.main()
