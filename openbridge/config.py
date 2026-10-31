"""Explicit configuration loading. Importing this module performs no I/O."""
import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from openbridge.adapters.execution.opencode import load_service


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Paths:
    home: Path
    service: Path

    @classmethod
    def discover(cls, default_home, environ=None):
        environ = os.environ if environ is None else environ
        home = Path(environ.get("OPENBRIDGE_HOME") or default_home).expanduser().absolute()
        service = Path(environ.get("OPENBRIDGE_SERVICE_JSON") or
                       "~/.local/state/opencode/service.json").expanduser()
        return cls(home, service)

    @property
    def config(self):
        return self.home / "config.json"


@dataclass(frozen=True)
class WorkerSettings:
    interactive: int = 1
    background: int = 1
    max_pending: int = 128
    per_conversation: int = 32

    @classmethod
    def from_config(cls, data):
        runtime = data.get("runtime") or {}
        values = {
            "interactive": runtime.get("interactive_workers", runtime.get("conversation_workers", 1)),
            "background": runtime.get("background_workers", 1),
            "max_pending": runtime.get("max_pending_messages", 128),
            "per_conversation": runtime.get("max_pending_per_conversation", 32),
        }
        for name, value in values.items():
            try:
                if isinstance(value, bool) or int(value) != float(value) or int(value) < 1:
                    raise ValueError
                values[name] = int(value)
            except (ValueError, TypeError, OverflowError):
                raise ConfigurationError(f"Invalid runtime worker setting: {name}") from None
        return cls(**values)


@dataclass(frozen=True)
class Configuration:
    paths: Paths
    data: dict = field(repr=False)  # Resolved credentials must not appear in repr.
    workers: WorkerSettings


def resolve_token(value, home):
    if isinstance(value, str) and value.startswith("{file:") and value.endswith("}"):
        try:
            return (Path(home) / value[6:-1]).read_text().strip()
        except (OSError, UnicodeError):
            return ""  # Missing credentials stay unavailable, never become a token literal.
    return value or ""


def load_config(paths):
    try:
        with paths.config.open() as handle:
            source = json.load(handle)
    except (OSError, ValueError):
        raise ConfigurationError("Cannot read valid OpenBridge configuration") from None
    if not isinstance(source, dict):
        raise ConfigurationError("OpenBridge configuration must be an object")
    data = copy.deepcopy(source)
    if not isinstance(data.get("bots", []), list) or not isinstance(data.get("owner_ids", []), list):
        raise ConfigurationError("bots and owner_ids must be lists")
    for key in ("runtime", "trigger", "budget", "rate_limiter", "tugas", "retention", "telegram"):
        if data.get(key) is not None and not isinstance(data[key], dict):
            raise ConfigurationError(f"{key} must be an object")
    for bot in data.get("bots", []):
        if not isinstance(bot, dict):
            raise ConfigurationError("Every bot configuration must be an object")
        token = resolve_token(bot.get("token"), paths.home)
        if not isinstance(token, str):
            raise ConfigurationError("Bot token must be text or a token-file reference")
        bot["token"] = token
    return Configuration(paths, data, WorkerSettings.from_config(data))


def load_registration(path):
    try:
        url, auth = load_service(path)
        if not url.startswith(("http://", "https://")) or not isinstance(auth[1], str):
            raise ValueError
        return url, auth
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        raise ConfigurationError(
            "Cannot read the OpenCode service registration. Start the OpenCode service "
            "or set OPENBRIDGE_SERVICE_JSON to its registration file.") from None
