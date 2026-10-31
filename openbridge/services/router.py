from runtime_policy import bot_id
from openbridge.services.background import BackgroundExecutor


class JobRouter:
    def __init__(self, accounts, config, store, jobs, conversations, gateway, send, call, output=None):
        self.accounts, self.config, self.store, self.jobs = accounts, config, store, jobs
        self.conversations, self.gateway, self.send, self.call = conversations, gateway, send, call
        self.output = output

    def execute(self, job, payload):
        bot = self.accounts(job["account_id"])
        if bot is None:
            raise RuntimeError("Job account is no longer configured")
        if job["kind"] == "telegram_update":
            if job["status_message_id"]:
                self.call(bot["token"], "editMessageText", {"chat_id": job["conversation_id"],
                    "message_id": job["status_message_id"], "text": "⚙️ Processing…"}, timeout=5)
            self.gateway.execute(bot, payload["update"], payload.get("username", ""))
            return
        if job["kind"] == "schedule":
            row = next((row for row in self.store.list_schedules() if row["id"] == payload["schedule_id"]), None)
            if not row or not row["active"] or str(row["bot_id"]) != job["account_id"] or str(row["chat_id"]) != job["conversation_id"]:
                return
        payload = dict(payload)
        if job["kind"] == "task":
            payload["wait_seconds"] = int((self.config.get("tugas") or {}).get("wait_detik", 240))
        BackgroundExecutor(self.jobs, lambda item: self.conversations.background_session(item, bot),
            lambda sid, prompt, timeout: self.conversations.background_model(bot, sid, prompt, timeout),
            lambda text: self.send(bot, job["conversation_id"], text),
            lambda path, caption, kind: self.output.send_file(bot_id(bot), job["conversation_id"], path, caption, kind)
            ).execute(job, payload)
