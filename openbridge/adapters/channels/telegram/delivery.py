"""Telegram formatting, wire payloads, and delivery outcome classification."""
from pathlib import Path

from openbridge.adapters.channels.telegram.uploads import post_multipart
from openbridge.adapters.channels.telegram.limits import retry_after


def outbound_path(home, raw):
    """Only deliver regular artifacts under hasil/, never the bridge home itself."""
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        root = home / 'hasil'
        if root.is_symlink():
            return None
        root = root.resolve()
        candidate = (home / raw).resolve()
        if home.resolve() not in root.parents or root not in candidate.parents:
            return None
        if not candidate.is_file() or candidate.stat().st_nlink != 1:
            return None
    except (ValueError, OSError, RuntimeError):
        return None
    return candidate


class TelegramDelivery:
    channel = "telegram"

    def __init__(self, call, resolve_account, enqueue_legacy, log, home=None, uploader=post_multipart, presence=None):
        self.call = call
        self.resolve_account = resolve_account
        self.enqueue_legacy = enqueue_legacy
        self.log = log
        self.home = home
        self.uploader = uploader
        self.presence = presence

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
            result = self.call(token, "sendMessage", {"chat_id": conversation_id, "text": chunk})
            if result.get("ok"):
                self.log("Delivered direct response to", conversation_id)
            elif result.get('not_sent') or (result.get('error_code') or result.get('error')) == 429:
                self.enqueue_legacy(token, conversation_id, chunk)
                self.log("Direct response queued in legacy outbox")
            else:
                self.log('Direct response outcome not confirmed; not replaying an ambiguous send')

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

    def deliver_file(self, account, delivery):
        """Upload a produced file; text never rides on the same wire."""
        if self.home is None:
            return "dead", "outbound files are not configured", None
        path = outbound_path(self.home, delivery["payload"]["path"])
        if path is None:
            self.log("Outbound file rejected", str(delivery["payload"]["path"])[:80])
            return "dead", "outbound file missing", None
        document = delivery["payload"]["operation"] == "document"
        method = "sendDocument" if document else "sendPhoto"
        fields = {"chat_id": str(delivery["conversation_id"])}
        caption = delivery["payload"].get("caption")
        if caption:
            fields["caption"] = str(caption)[:1024]
        response = self.uploader(account["token"], method, fields,
                                 "photo" if not document else "document", path, timeout=90)
        if response.get("ok"):
            self.log("Job delivery confirmed", delivery["job_id"], delivery["id"])
            return "sent", None, None
        code = response.get("error_code") or response.get("error")
        if response.get('not_sent'):
            return 'pending', 'Telegram connection was not established; request not sent', 2
        description = (response.get("description") or response.get("body") or "").lower()
        if code == 429:
            return "pending", "Telegram rate limit", retry_after(response)
        if isinstance(code, int) and 400 <= code < 500:
            return "dead", f"Telegram HTTP {code} ({description[:60]})", None
        return "uncertain", "Telegram delivery outcome unknown", None

    def send_file(self, account_id, conversation_id, path, caption=None, out_of_scope=True):
        """Direct (non-durable) file send for channel commands."""
        token = self._token(account_id)
        resolved = outbound_path(self.home, path) if self.home else None
        if resolved is None:
            return {"ok": False, "description": "outbound file missing"}
        document = not resolved.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
        method = "sendDocument" if document else "sendPhoto"
        fields = {"chat_id": str(conversation_id)}
        if caption:
            fields["caption"] = str(caption)[:1024]
        return self.uploader(token, method, fields, "document" if document else "photo", resolved, timeout=90)

    def deliver(self, delivery):
        if self.presence:
            return self.presence.finalize(delivery['job_id'], lambda: self._deliver(delivery))
        return self._deliver(delivery)

    def _deliver(self, delivery):
        account = self.resolve_account(delivery["account_id"])
        if account is None:
            return "dead", "account unavailable", None
        output = delivery["payload"]
        if output["operation"] in ("document", "photo"):
            return self.deliver_file(account, delivery)
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
        if response.get('not_sent'):
            return 'pending', 'Telegram connection was not established; request not sent', 2
        if code == 429:
            return "pending", "Telegram rate limit", retry_after(response)
        if isinstance(code, int) and 400 <= code < 500:
            return "dead", f"Telegram HTTP {code}", None
        return ("pending" if edit else "uncertain"), "Telegram delivery outcome unknown", None
