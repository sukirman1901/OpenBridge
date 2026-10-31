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

    def run_prompt(self, session_id: str, text: str, timeout=120) -> str:
        ...

    def session_idle(self, session_id: str) -> bool:
        ...

    def enforce_permissions(self, session_id: str, permissions: list):
        ...
