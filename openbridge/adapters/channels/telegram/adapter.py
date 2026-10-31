"""Telegram-specific wire formats stay outside the runtime."""
from openbridge.domain.messages import ChannelCapabilities, ConversationKey, IncomingMessage
from openbridge.ports.transport import JsonTransport


class TelegramClient:
    def __init__(self, transport: JsonTransport):
        self.transport = transport

    def call(self, token, method, payload=None, timeout=45):
        return self.transport(
            f"https://api.telegram.org/bot{token}/{method}",
            "POST" if payload else "GET", payload, timeout=timeout)


class TelegramAdapter:
    capabilities = ChannelCapabilities(edit_messages=True, buttons=True, typing=True)

    def __init__(self, client: TelegramClient, token: str):
        self.client = client
        self._token = token

    @staticmethod
    def normalize(account_id: str, message: dict) -> IncomingMessage:
        return IncomingMessage(
            conversation=ConversationKey("telegram", str(account_id), str(message["chat"]["id"])),
            message_id=str(message["message_id"]),
            sender_id=str(message["from"]["id"]),
            text=(message.get("text") or message.get("caption") or "").strip(),
        )

    def send_text(self, conversation_id: str, text: str) -> dict:
        return self.client.call(self._token, "sendMessage", {
            "chat_id": conversation_id, "text": text})
