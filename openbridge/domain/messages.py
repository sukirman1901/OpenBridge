"""Transport-neutral message identities. Credentials are never identity fields."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ConversationKey:
    channel: str
    account_id: str
    conversation_id: str

    def __post_init__(self):
        for name in ("channel", "account_id", "conversation_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")


@dataclass(frozen=True)
class IncomingMessage:
    conversation: ConversationKey
    message_id: str
    sender_id: str
    text: str


@dataclass(frozen=True)
class ChannelCapabilities:
    edit_messages: bool = False
    buttons: bool = False
    typing: bool = False
