"""Telegram formatting, wire payloads, and delivery outcome classification."""
import json


class TelegramDelivery:
    channel = "telegram"

    def __init__(self, call, resolve_account, enqueue_legacy, log):
        self.call = call
        self.resolve_account = resolve_account
        self.enqueue_legacy = enqueue_legacy
        self.log = log

    @staticmethod
    def keyboard(buttons, columns=2):
        columns = max(1, int(columns))
        return {"inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in buttons[index:index + columns]]
            for index in range(0, len(buttons), columns)]}

    def formatted_call(self, token, method, payload, parse="Markdown", timeout=45):
        request = dict(payload, parse_mode=parse)
        result = self.call(token, method, request, timeout=timeout)
        description = (result.get("description") or result.get("body") or "").lower()
        code = result.get("error_code") or result.get("error")
        if not result.get("ok") and code == 400 and "parse" in description:
            request.pop("parse_mode")
            result = self.call(token, method, request, timeout=timeout)
            self.log("Telegram formatting rejected; plain-text fallback used")
        return result

    def _token(self, account_id):
        account = self.resolve_account(str(account_id))
        if account is None:
            raise RuntimeError("Delivery account is unavailable")
        return account["token"]

    def send_text(self, account_id, conversation_id, text, buttons=None, columns=2):
        token = self._token(account_id)
        if buttons:
            result = self.call(token, "sendMessage", {"chat_id": conversation_id, "text": text,
                               "reply_markup": self.keyboard(buttons, columns)})
            if not result.get("ok"):
                self.log("Telegram panel send failed")
            return result
        for index in range(0, len(text), 3500):
            chunk = text[index:index + 3500]
            result = self.formatted_call(token, "sendMessage", {"chat_id": conversation_id, "text": chunk})
            if result.get("ok"):
                self.log("Delivered direct response to", conversation_id)
            else:
                self.enqueue_legacy(token, conversation_id, chunk)
                self.log("Direct response queued in legacy outbox")

    def edit_text(self, account_id, conversation_id, message_id, text, buttons=None, columns=2, parse=False):
        token = self._token(account_id)
        request = {"chat_id": conversation_id, "message_id": message_id, "text": text[:4000]}
        if buttons is not None:
            request["reply_markup"] = self.keyboard(buttons, columns)
        result = (self.formatted_call(token, "editMessageText", request) if parse and not buttons else
                  self.call(token, "editMessageText", request))
        if not result.get("ok"):
            self.log("Telegram direct edit failed")
        return result

    def deliver(self, delivery):
        account = self.resolve_account(delivery["account_id"])
        if account is None:
            return "dead", "account unavailable", None
        output = delivery["payload"]
        if output["operation"] not in ("send", "edit"):
            return "dead", "unsupported delivery operation", None
        edit = output["operation"] == "edit"
        method = "editMessageText" if edit else "sendMessage"
        request = {"chat_id": delivery["conversation_id"], "text": output["text"]}
        if edit:
            request["message_id"] = output["message_id"]
        buttons = output.get("buttons")
        if buttons is not None or edit:
            request["reply_markup"] = self.keyboard(buttons or [], output.get("cols", 2))
        if output.get("parse"):
            response = self.formatted_call(account["token"], method, request, timeout=10)
        else:
            response = self.call(account["token"], method, request, timeout=10)
        description = (response.get("description") or response.get("body") or "").lower()
        if response.get("ok") or (edit and "message is not modified" in description):
            self.log("Job delivery confirmed", delivery["job_id"], delivery["id"])
            return "sent", None, None
        code = response.get("error_code") or response.get("error")
        if code == 429:
            try:
                details = response if response.get("parameters") else json.loads(response.get("body", "{}"))
                raw_delay = (details.get("parameters") or {}).get("retry_after", 30)
                delay = int(raw_delay)
                if isinstance(raw_delay, bool) or delay < 1:
                    delay = 30
            except (ValueError, TypeError, AttributeError, OverflowError):
                delay = 30
            return "pending", "Telegram rate limit", delay
        if isinstance(code, int) and 400 <= code < 500:
            return "dead", f"Telegram HTTP {code}", None
        return ("pending" if edit else "uncertain"), "Telegram delivery outcome unknown", None
