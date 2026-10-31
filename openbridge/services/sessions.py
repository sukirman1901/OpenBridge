"""Session ownership and preparation with injected backend/policy boundaries."""
from openbridge.ports.execution import ExecutionBackend


class ConversationSessions:
    def __init__(self, cache, lock, open_session, prompt, register, checkpoint, log,
                 prepared=None, mark_prepared=None):
        self.cache = cache
        self.lock = lock
        self.open_session = open_session
        self.prompt = prompt
        self.register = register
        self.checkpoint = checkpoint
        self.log = log
        self.prepared, self.mark_prepared = prepared, mark_prepared

    def _attach(self, sid):
        self.register(sid)
        self.checkpoint(sid)
        return sid

    def get(self, key, title, briefing):
        if key in self.cache:
            return self._attach(self.cache[key])
        with self.lock:
            if key in self.cache:
                return self._attach(self.cache[key])
            sid = self.open_session(title)
            self._attach(sid)  # Persist/register before any model call.
            text = briefing()
            if not self.prepared or not self.prepared(sid, text):
                acknowledgement = self.prompt(sid, text, 90)
                if self.mark_prepared:
                    self.mark_prepared(sid, text)
                self.log("Conversation session prepared", sid, "briefing_ready", bool(acknowledgement))
            self.cache[key] = sid
            return sid


class BackgroundSessions:
    def __init__(self, backend: ExecutionBackend, directory, permissions, register, checkpoint):
        self.backend = backend
        self.directory = directory
        self.permissions = permissions
        self.register = register
        self.checkpoint = checkpoint

    def get(self, job):
        sid = job["session_id"]
        if sid:
            self.backend.enforce_permissions(sid, self.permissions())
        else:
            sid = self.backend.create_session(f"openbridge-{job['kind']}-{job['id']}",
                                              self.directory, self.permissions())
        self.register(sid)
        self.checkpoint(sid)
        return sid
