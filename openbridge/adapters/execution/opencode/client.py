"""OpenCode V2 session API adapter. It does not own conversation scheduling."""
import json
import time
from urllib.parse import quote

from openbridge.ports.transport import JsonTransport
from openbridge.domain.errors import ExecutionUncertain, RetryLater


def load_service(path):
    with open(path) as handle:
        service = json.load(handle)
    return service["url"].rstrip("/"), ("opencode", service["password"])


class OpenCodeClient:
    def __init__(self, url: str, auth: tuple, transport: JsonTransport, events=None):
        self.url = url.rstrip("/")
        self.auth = auth
        self.transport = transport
        self.events = events

    def request(self, path, method="GET", body=None, timeout=45):
        if not path.startswith("/api/"):
            raise ValueError("OpenCode requests must use an /api/ path")
        return self.transport(self.url + path, method, body, self.auth, timeout=timeout)

    @staticmethod
    def session_path(session_id):
        return "/api/session/" + quote(session_id, safe="")

    @staticmethod
    def require_success(result):
        if result.get("error"):
            # Do not include a transport body: it may contain prompt data.
            raise RuntimeError("OpenCode request failed")
        return result

    def create_session(self, title, directory, permissions):
        result = self.require_success(self.request("/api/session", "POST", {
            "title": title, "location": {"directory": directory},
            "permissions": permissions}))
        session_id = (result.get("data") or result).get("id")
        if not session_id:
            raise RuntimeError("OpenCode returned no session ID")
        return session_id

    def open_session(self, title, directory, permissions):
        result = self.require_success(self.request("/api/session"))
        sessions = result.get("data")
        if not isinstance(sessions, list):
            raise RuntimeError("OpenCode returned no valid session list")
        for session in sessions:
            if session.get("title") == title:
                self.enforce_permissions(session["id"], permissions)
                return session["id"]
        return self.create_session(title, directory, permissions)

    def messages(self, session_id):
        result = self.require_success(self.request(self.session_path(session_id) + "/message"))
        return result.get("data") or []

    def submit_prompt(self, session_id, text):
        return self.require_success(self.request(
            self.session_path(session_id) + "/prompt", "POST", {"text": text}))

    def session_idle(self, session_id):
        try:
            active = self.request("/api/session/active")
            inbox = self.request(self.session_path(session_id) + "/inbox")
            return (not active.get("error") and not inbox.get("error")
                    and isinstance(active.get("data"), dict) and isinstance(inbox.get("data"), list)
                    and session_id not in active["data"] and not inbox["data"])
        except Exception:
            return False  # Unknown backend state must never release a fence.

    def enforce_permissions(self, session_id, permissions):
        return self.require_success(self.request(self.session_path(session_id), "PATCH", {"permissions": permissions}))

    def session_info(self, sid):
        return self.require_success(self.request(self.session_path(sid))).get("data") or {}

    def delete_session(self, sid):
        return self.require_success(self.request(self.session_path(sid), "DELETE"))

    def compact_session(self, sid, timeout=600, on_accepted=None, on_state=None,
                        clock=time.monotonic, wait=time.sleep):
        # V2 /compact admits work asynchronously; a successful POST does not
        # mean a new prompt can safely be submitted to this session yet.
        active = self.require_success(self.request('/api/session/active')).get('data')
        if not isinstance(active, dict) or sid in active:
            raise ExecutionUncertain('Session is not available for compaction')
        response = self.request(self.session_path(sid) + '/compact', 'POST', {})
        if response.get('error'):
            raise ExecutionUncertain('Compaction acceptance is unknown')
        receipt = (response.get('data') or {}).get('id')
        if not receipt:
            raise ExecutionUncertain('Compaction returned no receipt')
        if on_accepted:
            try:
                on_accepted(receipt)
            except Exception as error:
                raise ExecutionUncertain('Compaction receipt could not be persisted') from error
        deadline = clock() + timeout
        while clock() < deadline:
            result = self.completion(sid, receipt)
            if result and self.session_idle(sid):
                if result['outcome'] != 'succeeded':
                    raise RuntimeError('Compaction failed')
                return response
            wait(1)
        raise ExecutionUncertain('Compaction timed out')

    def rename_session(self, sid, title):
        return self.require_success(self.request(self.session_path(sid), "PATCH", {"title": title}))

    def interrupt(self, sid):
        return self.require_success(self.request(self.session_path(sid) + "/interrupt", "POST", {}))

    def models(self):
        return self.require_success(self.request("/api/model")).get("data") or []

    def agents(self):
        data = self.require_success(self.request("/api/agent")).get("data") or {}
        if isinstance(data, dict):
            return list(data)
        names = [item.get("id") or item.get("name") if isinstance(item, dict) else item for item in data]
        return [name for name in names if name]

    def set_model(self, sid, provider, model):
        return self.require_success(self.request(self.session_path(sid) + "/model", "POST",
                                                {"model": {"id": model, "providerID": provider}}))

    def set_agent(self, sid, agent):
        return self.require_success(self.request(self.session_path(sid) + "/agent", "POST", {"agent": agent}))

    def completion(self, session_id, message_id):
        """Read only the terminal result belonging to this exact accepted user ID."""
        messages = sorted(self.messages(session_id), key=lambda m: m.get('time', {}).get('created', 0))
        accepted = next((i for i, m in enumerate(messages)
                         if m.get('id') == message_id and m.get('type') in ('user', 'compaction')), None)
        if accepted is None:
            return None
        fresh = []
        for message in messages[accepted + 1:]:
            if message.get('type') in ('user', 'compaction'):
                break  # A later user's result never belongs to this receipt.
            fresh.append(message)
        tools_used = any(m.get('finish') == 'tool-calls' or
                         any(p.get('type') == 'tool' for p in m.get('content', [])) for m in fresh)
        terminal = next((i for i, m in enumerate(fresh) if m.get('type') == 'idle'), None)
        if terminal is not None:
            outcome = fresh[terminal].get('outcome')
            if outcome not in ('succeeded', 'failed', 'interrupted', 'cancelled'):
                return None
            answers = [' '.join(p.get('text', '') for p in m.get('content', []) if p.get('type') == 'text').strip()
                       for m in fresh[:terminal] if m.get('type') == 'assistant' and m.get('finish') != 'tool-calls']
            return {'outcome': 'succeeded' if outcome == 'succeeded' else 'failed',
                    'answer': '\n\n'.join(t for t in answers if t), 'error': fresh[terminal].get('error'),
                    'tools_used': tools_used}
        if fresh and fresh[-1].get('type') == 'assistant' and fresh[-1].get('finish') == 'error':
            return {'outcome': 'failed', 'answer': '', 'error': fresh[-1].get('error'),
                    'requires_idle': True, 'tools_used': tools_used}
        return None

    def run_prompt(self, session_id, text, timeout=120, clock=time.monotonic, wait=time.sleep,
                   on_accepted=None, on_state=None, approval_timeout=300):
        if self.events:
            self.events.watch(session_id)
        try:
            return self._run_prompt(session_id, text, timeout, clock, wait, on_accepted, on_state, approval_timeout)
        finally:
            if self.events:
                self.events.unwatch(session_id)

    def _run_prompt(self, session_id, text, timeout, clock, wait, on_accepted, on_state, approval_timeout):
        """Wait for this accepted user message's terminal idle, never partial text.

        V2 prompt acceptance returns a user message ID, not a completed answer.
        Timeouts and transport failures after acceptance require reconciliation.
        """
        active = self.require_success(self.request("/api/session/active"))
        if not isinstance(active.get('data'), dict):
            raise RuntimeError('OpenCode returned no valid active-session inventory')
        if session_id in (active.get("data") or {}):
            raise ExecutionUncertain("OpenCode session is already active")
        response = self.request(self.session_path(session_id) + "/prompt", "POST", {"text": text})
        if response.get("error"):
            code = response["error"]
            if isinstance(code, int) and 400 <= code < 500 and code != 409:
                raise RuntimeError("OpenCode rejected the prompt")
            raise ExecutionUncertain("OpenCode prompt acceptance is unknown")
        message_id = (response.get("data") or {}).get("id")
        if not message_id:
            raise ExecutionUncertain("OpenCode returned no accepted message ID")
        if on_accepted:
            try:
                on_accepted(message_id)
            except Exception as error:
                raise ExecutionUncertain('OpenCode receipt could not be persisted') from error
        start = previous = clock()
        work_left, approval_left = float(timeout), float(approval_timeout)
        hard_deadline = start + timeout + approval_timeout
        errors, state, last_permission = 0, 'working', start - 5
        while clock() < hard_deadline and work_left > 0 and approval_left > 0:
            now = clock()
            elapsed = max(0, now - previous)
            previous = now
            if state == 'waiting_approval':
                approval_left -= elapsed
            else:
                work_left -= elapsed
            if on_state and now - last_permission >= 5:
                last_permission = now
                permissions = self.request(self.session_path(session_id) + '/permission')
                pending = permissions.get('data')
                state = ('waiting_approval' if pending else 'working') if isinstance(pending, list) else 'checking_backend'
                on_state(state)
            try:
                result = self.completion(session_id, message_id)
                errors = 0
            except Exception as error:
                errors += 1
                if on_state:
                    on_state('checking_backend')
                if errors >= 3:
                    raise ExecutionUncertain('OpenCode result lookup failed') from error
                result = None
            if result:
                if result.get('requires_idle') and not self.session_idle(session_id):
                    raise ExecutionUncertain('OpenCode failed but backend state needs review')
                if result['outcome'] != 'succeeded':
                    failure = result.get('error')
                    if not result.get('tools_used') and isinstance(failure, dict) and ('limit' in str(failure.get('type', '')).lower()
                                                     or 'quota' in str(failure.get('type', '')).lower()):
                        raise RetryLater(900)
                    raise RuntimeError('OpenCode execution did not succeed')
                if not result['answer']:
                    raise RuntimeError('OpenCode completed without a text answer')
                return result['answer']
            if self.events:
                self.events.wait(session_id, 1)
                wait(.25)  # Coalesce token events rather than querying history per token.
            else:
                wait(1)
        raise ExecutionUncertain("OpenCode result timed out; session may still be active")
