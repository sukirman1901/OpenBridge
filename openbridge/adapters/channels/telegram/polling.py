"""Polling uses injected acceptance/storage boundaries, not bridge globals."""
from openbridge.adapters.channels.telegram.limits import retry_after

COMMANDS = [
    {"command": command, "description": description} for command, description in (
        ("help", "📖 Command menu"), ("model", "🧠 Swap the brain (model)"),
        ("agent", "🎭 Switch the machine agent"), ("persona", "👥 View/manage personas"),
        ("jadwal", "⏰ Automatic schedules"), ("tugas", "📋 Hand off / track tasks"),
        ("status", "📊 Bot status"), ("jobs", "📋 Durable job status"),
        ("job", "📋 Job status (alias)"), ("resolve", "🔎 Reconcile an interrupted job"),
        ("result", "📄 Retrieve a saved job result"), ("new", "♻️ Fresh context (summary carried over)"),
        ("reset", "🆕 Wipe memory"), ("cancel", "🚫 Cancel a queued job"),
        ("remember", "🧠 Teach a durable fact"), ("memory", "🧠 List stored facts"),
        ("forget", "🗑️ Forget one stored fact"))]


class TelegramPoller:
    def __init__(self, call, jobs, legacy_store, accept, stopped, log, poll_seconds=25):
        self.call = call
        self.jobs = jobs
        self.legacy_store = legacy_store
        self.accept = accept
        self.stopped = stopped
        self.log = log
        self.poll_seconds = poll_seconds

    def run(self, bot):
        delay = 1
        while not self.stopped.is_set():
            identity = self.call(bot['token'], 'getMe')
            if identity.get('ok'):
                break
            self.log("Telegram identity check failed for", bot["nama"])
            code = identity.get('error_code') or identity.get('error')
            if code in (401, 404):
                return
            self.stopped.wait(retry_after(identity) if code == 429 else delay)
            delay = min(30, delay * 2)
        else:
            return
        bot["bot_id"] = str(identity["result"]["id"])
        bot["username"] = identity["result"].get("username")
        self.call(bot["token"], "setMyCommands", {"commands": COMMANDS})
        account_id = bot["bot_id"]
        offset = self.jobs.cursor("telegram", account_id, self.legacy_store.get_offset(account_id))
        self.log("Telegram bot online:", bot["nama"], "offset", offset)
        backoff = 1
        while not self.stopped.is_set():
            response = self.call(bot["token"], "getUpdates", {"offset": offset, "timeout": self.poll_seconds},
                                 timeout=self.poll_seconds + 10)
            if not response.get('ok'):
                code = response.get('error_code') or response.get('error')
                self.stopped.wait(retry_after(response) if code == 429 else backoff)
                backoff = min(30, backoff * 2)
                continue
            backoff = 1
            updates = response.get('result') or []
            for update in updates:
                if self.stopped.is_set():
                    return  # Unaccepted updates remain at the source cursor.
                self.accept(bot, update)
                offset = self.jobs.cursor("telegram", account_id)
                self.legacy_store.set_offset(account_id, offset)
            self.stopped.wait(.05 if updates else .5)
