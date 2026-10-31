"""Channel output boundary; runtime coordination never needs a bot token."""
from typing import Protocol


class DeliveryChannel(Protocol):
    channel: str

    def send_text(self, account_id, conversation_id, text, buttons=None, columns=2):
        ...

    def edit_text(self, account_id, conversation_id, message_id, text,
                  buttons=None, columns=2, parse=False):
        ...

    def deliver(self, delivery) -> tuple:
        ...
