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
    def __init__(self, url: str, auth: tuple, transport: JsonTransport):
        self.url = url.rstrip("/")
        self.auth = auth
        self.transport = transport

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

    def compact_session(self, sid):
        return self.require_success(self.request(self.session_path(sid) + "/compact", "POST", {}))

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

    def run_prompt(self, session_id, text, timeout=120, clock=time.monotonic, wait=time.sleep):
        """Wait for this accepted user message's terminal idle, never partial text.

        V2 prompt acceptance returns a user message ID, not a completed answer.
        Timeouts and transport failures after acceptance require reconciliation.
        """
        active = self.require_success(self.request("/api/session/active"))
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
        deadline = clock() + timeout
        while clock() < deadline:
            try:
                messages = sorted(self.messages(session_id),
                                  key=lambda message: message.get("time", {}).get("created", 0))
            except Exception as error:
                raise ExecutionUncertain("OpenCode result lookup failed") from error
            accepted = next((index for index, message in enumerate(messages)
                             if message.get("id") == message_id), None)
            if accepted is not None:
                fresh = messages[accepted + 1:]
                terminal = next((index for index, message in enumerate(fresh)
                                 if message.get("type") == "idle"), None)
                if terminal is not None:
                    if fresh[terminal].get("outcome") != "succeeded":
                        failure = fresh[terminal].get("error")
                        if isinstance(failure, dict) and ("limit" in str(failure.get("type", "")).lower()
                                                          or "quota" in str(failure.get("type", "")).lower()):
                            # Provider quota, not work gone wrong: the answer
                            # never ran, so deferring the whole job is safe
                            # and replayable at the next pump tick.
                            raise RetryLater(900)
                        raise RuntimeError("OpenCode execution did not succeed")
                    answers = [" ".join(part.get("text", "") for part in message.get("content", [])
                                         if part.get("type") == "text").strip()
                               for message in fresh[:terminal] if message.get("type") == "assistant"
                               and message.get("finish") != "tool-calls"]
                    answer = "\n\n".join(text for text in answers if text)
                    if not answer:
                        raise RuntimeError("OpenCode completed without a text answer")
                    return answer
            wait(1)
        raise ExecutionUncertain("OpenCode result timed out; session may still be active")
