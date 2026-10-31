"""Polling uses injected acceptance/storage boundaries, not bridge globals."""

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
        identity = self.call(bot["token"], "getMe")
        if not identity.get("ok"):
            self.log("Telegram identity check failed for", bot["nama"])
            return
        bot["bot_id"] = str(identity["result"]["id"])
        bot["username"] = identity["result"].get("username")
        self.call(bot["token"], "setMyCommands", {"commands": COMMANDS})
        account_id = bot["bot_id"]
        offset = self.jobs.cursor("telegram", account_id, self.legacy_store.get_offset(account_id))
        self.log("Telegram bot online:", bot["nama"], "offset", offset)
        while not self.stopped.is_set():
            response = self.call(bot["token"], "getUpdates", {"offset": offset, "timeout": self.poll_seconds},
                                 timeout=self.poll_seconds + 10)
            updates = response.get("result") or [] if response.get("ok") else []
            for update in updates:
                if self.stopped.is_set():
                    return  # Unaccepted updates remain at the source cursor.
                self.accept(bot, update)
                offset = self.jobs.cursor("telegram", account_id)
                self.legacy_store.set_offset(account_id, offset)
            self.stopped.wait(.5)
