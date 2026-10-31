"""JSON transport boundary used by external service clients."""
from typing import Protocol


class JsonTransport(Protocol):
    def __call__(self, url, method="GET", body=None, auth=None, timeout=45) -> dict:
        ...
