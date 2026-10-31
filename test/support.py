"""Explicit isolated application factory for integration tests, never production state."""
import json
from pathlib import Path
import tempfile
from unittest.mock import Mock

from openbridge.bootstrap import create_application
from openbridge.config import Paths, load_config


def make_application(test, *, backend=None, call=None, changes=None, async_presence=False):
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    home = Path(temporary.name)
    token = "123:" + "x" * 32
    (home / "bot.token").write_text(token)
    data = {"bots": [{"nama": "Una", "persona": "helpful", "token": "{file:bot.token}"}],
            "owner_ids": [42], "trigger": {}, 'telegram': {'acknowledge': True}}
    data.update(changes or {})
    (home / "config.json").write_text(json.dumps(data))
    (home / "opencode.jsonc").write_text(json.dumps({"permissions": [{"action": "*", "resource": "*", "effect": "deny"}]}))
    if backend is None:
        backend = Mock()
        backend.open_session.return_value = "session-test"
        backend.create_session.return_value = "background-session"
        backend.run_prompt.return_value = "ready"
        backend.session_info.return_value = {}
        backend.models.return_value = []
        backend.agents.return_value = []
        backend.session_idle.return_value = True
    app = create_application(load_config(Paths(home, home / "service.json")), backend=backend,
                              channel_call=call or Mock(return_value={"ok": True}), log=Mock())
    if async_presence:
        app.components.presence.start()
    else:
        # Unit tests drive runtime.run directly without Application.run loops.
        # Keep ACK setup deterministic; stress/integration tests opt into the
        # real bounded asynchronous worker explicitly.
        def acknowledge(identity, bot, chat, text, callback):
            response = app.components.gateway.call(bot['token'], 'sendMessage', {'chat_id': chat, 'text': text})
            callback((response.get('result') or {}).get('message_id') if response.get('ok') else None)
        app.components.presence.acknowledge = acknowledge
    test.addCleanup(app.close)
    return app
