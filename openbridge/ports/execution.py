from typing import Protocol


class ExecutionBackend(Protocol):
    def create_session(self, title: str, directory: str, permissions: list) -> str:
        ...

    def open_session(self, title: str, directory: str, permissions: list) -> str:
        ...

    def messages(self, session_id: str) -> list:
        ...

    def submit_prompt(self, session_id: str, text: str) -> dict:
        ...

    def run_prompt(self, session_id: str, text: str, timeout=120,
                   on_accepted=None, on_state=None, approval_timeout=300) -> str:
        ...

    def completion(self, session_id: str, message_id: str):
        """Return the terminal result for an exact receipt, or None if unknown."""
        ...

    def session_idle(self, session_id: str) -> bool:
        ...

    def enforce_permissions(self, session_id: str, permissions: list):
        ...
