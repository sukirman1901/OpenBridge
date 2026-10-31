"""Telegram ingestion and saved-update handling. No module-level runtime state."""
import threading

from runtime_policy import bot_id
from openbridge.services.untrusted import framed
from openbridge.adapters.channels.telegram import TelegramAdapter
from openbridge.adapters.channels.telegram.polling import TelegramPoller
from openbridge.services.conversations import conversation_key


class TelegramGateway:
    def __init__(self, *, config, call, jobs, store, runtime, context, conversations, commands,
                 media, schedules, authorize, send, controls, stopped, log):
        self.config, self.call, self.jobs, self.store, self.runtime = config, call, jobs, store, runtime
        self.context, self.conversations, self.commands, self.media, self.schedules = context, conversations, commands, media, schedules
        self.authorize, self.send, self.controls, self.stopped, self.log = authorize, send, controls, stopped, log

    def poll(self, bot, index=0):
        return TelegramPoller(self.call, self.jobs, self.store, self.accept, self.stopped, self.log).run(bot)

    @staticmethod
    def addressed(message, username, text):
        return (message["chat"].get("type") == "private" or (bool(username) and f"@{username}" in text)
                or (bool(username) and (message.get("reply_to_message") or {}).get("from", {}).get("username") == username))

    def accept(self, bot, update):
        callback = update.get("callback_query")
        message = (callback or {}).get("message") or update.get("message") or update.get("edited_message") or {}
        sender = (callback or message).get("from") or {}
        chat = message.get("chat") or {}
        permitted = bool(chat.get("id")) and self.authorize(sender)
        text = (message.get("text") or "").strip()
        command = text.split(maxsplit=1)[0].split("@")[0] if text else ""
        control = bool(callback and callback.get("data", "").startswith(("ap-y-", "ap-n-", "st-")))
        management = not callback and chat.get("type") == "private" and command in ("/job", "/jobs", "/resolve", "/result", "/cancel")
        job_id, created = self.jobs.accept(conversation_key(bot, chat.get("id", "ignored")), update["update_id"],
            {"update": update, "username": bot.get("username", "")} if permitted else {}, enqueue=permitted and not (control or management))
        if not created or not permitted:
            return
        if management:
            self.controls(bot, chat["id"], text)
            return
        if control:
            threading.Thread(target=self.commands.callback, args=(bot, callback), daemon=True).start()
            return
        acknowledgement = None
        try:
            if not callback and self.addressed(message, bot.get("username"), text):
                result = self.call(bot["token"], "sendMessage", {"chat_id": chat["id"],
                    "text": f"⏳ Job #{job_id} received. I'll process it in order."}, timeout=8)
                acknowledgement = (result.get("result") or {}).get("message_id") if result.get("ok") else None
        finally:
            self.jobs.acknowledge(job_id, acknowledgement)
        self.log("Persisted Telegram job", job_id)

    def typing_action(self, bot, chat_id, done):
        """Best-effort typing indicator while the agent works; stops on reply."""
        while not done.is_set():
            try:
                self.call(bot["token"], "sendChatAction",
                          {"chat_id": chat_id, "action": "typing"}, timeout=5)
            except Exception:
                return  # typing is cosmetic; never let it break the job
            done.wait(4)

    def execute(self, bot, update, username):
        callback = update.get("callback_query")
        if callback:
            if self.authorize(callback.get("from") or {}):
                self.commands.callback(bot, callback)
            return
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if not chat.get("id") or not self.authorize(message.get("from") or {}):
            return
        incoming = TelegramAdapter.normalize(bot_id(bot), message)
        text, sender = incoming.text, (message.get("from") or {}).get("first_name") or "anon"
        attachment, local = self.media.receive(bot, message)  # Always after current-policy authorization.
        if attachment:
            text = (f"{text}\n\n" + framed("ATTACHMENT", sender, attachment)
                    + "\nImages require vision support; video/audio cannot be heard or watched.").strip()
        if not text:
            return
        chat_id = chat["id"]
        self.store.save_chat(chat_id, chat.get("title") or f"DM-{sender}")
        addressed = self.addressed(message, username, text)
        self.context.append(chat_id, "user", text, name=sender)
        if text.startswith("/") and addressed:
            parts = text.split(maxsplit=1)
            self.commands.handle(bot, chat_id, parts[0].lstrip("/").split("@")[0], parts[1] if len(parts) > 1 else "")
            return
        typing_done = threading.Event()
        threading.Thread(target=self.typing_action, args=(bot, chat_id, typing_done), daemon=True).start()
        try:
            scheduled = self.schedules.interpret(bot_id(bot), chat_id, text, addressed)
            if scheduled:
                self.send(bot, chat_id, scheduled)
                return
            if not addressed:
                job = self.runtime.current_job()
                if job:
                    self.jobs.note_initiative(conversation_key(bot, chat_id), job["id"],
                        self.context.format_context(chat_id, self.config.get("trigger", {}).get("konteks_n", 15)))
                return
            self.conversations.answer(bot, chat_id, sender, text, chat.get("type"))
        finally:
            typing_done.set()
