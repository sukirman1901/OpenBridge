from typing import Protocol

from openbridge.domain.messages import ChannelCapabilities, IncomingMessage


class ChannelAdapter(Protocol):
    capabilities: ChannelCapabilities

    def normalize(self, account_id: str, message: dict) -> IncomingMessage:
        ...

    def send_text(self, conversation_id: str, text: str) -> dict:
        ...
