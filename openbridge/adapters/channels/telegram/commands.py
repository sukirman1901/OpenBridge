"""Owner-only Telegram commands and callbacks with injected application services."""
import re
import time

from runtime_policy import bot_id
from openbridge.services.conversations import session_key
from openbridge.services.routines import format_expr


class TelegramCommands:
    def __init__(self, *, config, backend, conversations, personas, schedules, store, tasks,
                 jobs, approvals, provision, authorize, send, edit, call, log):
        self.config, self.backend, self.conversations = config, backend, conversations
        self.personas, self.schedules, self.store, self.tasks = personas, schedules, store, tasks
        self.jobs, self.approvals, self.provision = jobs, approvals, provision
        self.authorize, self.send, self.edit, self.call, self.log = authorize, send, edit, call, log
        self.cache = {}
        self.started = time.time()

    def panel(self, bot, chat_id, kind, message_id=None):
        cache = self.cache.setdefault(bot_id(bot), {})
        buttons, columns = None, 2
        if kind == "model":
            cache["models"] = self.backend.models()
            buttons = [(str(row.get("name") or row.get("modelID"))[:26], f"md-{index}")
                       for index, row in enumerate(cache["models"])]
            text = "🧠 Pick the brain (model):"
        elif kind == "agent":
            cache["agents"] = self.backend.agents()
            buttons = [(name, f"ag-{index}") for index, name in enumerate(cache["agents"][:24])]
            columns, text = 3, "🎭 Pick the machine agent:"
        elif kind == "persona":
            text = "👥 Active personas:\n" + "\n".join(f"• {row['nama']} — {str(row['persona'])[:50]}" for row in self.personas.list())
        else:
            rows = [row for row in self.schedules.list(active_only=False)
                    if row["bot_id"] == bot_id(bot) and int(row["chat_id"]) == int(chat_id)]
            text = ("⏰ This chat's routines:\n" + "\n".join(
                f"• #{row['id']} {'⏸ disabled' if not row['active'] else row['kind']} "
                f"{format_expr({'kind': row['kind'], 'expr': row['jam']})} — {row['prompt'][:40]}"
                for row in rows)
                if rows else "🗓️ No routine in this chat yet. Try 'remind me tomorrow at 5' or 'jadwalkan konten jam 8'.")
            text += "\nPause: 'jeda jadwal 2' · Resume: 'lanjutkan jadwal 2' · Edit: 'ubah jadwal 2 ke jam 9'."
            buttons = [(f"Del #{row['id']}", f"jd-{row['id']}") for row in rows] or None
        if message_id is None:
            self.send(bot, chat_id, text, buttons=buttons, cols=columns)
        else:
            self.edit(bot, chat_id, message_id, text, buttons=buttons, cols=columns)

    def set_model_text(self, bot, chat_id, text):
        key = lambda value: re.sub(r"[^a-z0-9]", "", str(value).lower())
        for model in self.backend.models():
            names = (model.get("name"), model.get("modelID"), f"{model.get('providerID')}/{model.get('modelID')}")
            if key(text) in [key(name) for name in names if name]:
                sid = self.conversations.get(bot, chat_id)
                self.backend.set_model(sid, model["providerID"], model["modelID"])
                self.send(bot, chat_id, f"🧠 Brain swapped to {model['providerID']}/{model['modelID']}")
                return
        self.send(bot, chat_id, "Model not found. Use /model to pick one.")

    def handle(self, bot, chat_id, command, argument="", persona=None):
        persona = persona or {"nama": bot["nama"], "persona": bot.get("persona", "")}
        if command in ("help", "bantuan", "menu"):
            self.send(bot, chat_id, "📖 Commands: /model /agent /persona /jadwal /tugas /status /new /reset\n"
                "Private job controls: /jobs /job <id> /result <id> /cancel <id> /resolve <id>.\n"
                "Routines by chat: 'remind me tomorrow at 5', 'tiap senin jam 8', "
                "'jeda jadwal 2', 'lanjutkan jadwal 2', 'ubah jadwal 2 ke jam 9', "
                "'batalkan jadwal 2', 'batalkan semua jadwal'.", buttons=[
                ("Model", "c-md"), ("Agent", "c-persona"), ("Schedules", "c-jd"),
                ("Personas", "c-ps"), ("Status", "c-st"), ("Reset", "c-rs")])
        elif command in ("model", "md") and argument:
            self.set_model_text(bot, chat_id, argument)
        elif command in ("model", "agent", "persona", "jadwal"):
            self.panel(bot, chat_id, command)
        elif command == "tugas":
            if argument:
                maximum = int((self.config.get("tugas") or {}).get("max_steps", 10))
                if not 1 <= maximum <= 100:
                    self.send(bot, chat_id, "Task step budget must be between 1 and 100.")
                    return
                identity = self.tasks.add_task(bot_id(bot), chat_id, argument[:500], max_steps=maximum)
                job_id = self.jobs.enqueue_task(identity, bot_id(bot))
                self.send(bot, chat_id, f"📋 Task #{identity} queued as job #{job_id} ({maximum} steps). Use /job {job_id}.")
            else:
                rows = self.tasks.list_tasks(bot_id=bot_id(bot), limit=10)
                self.send(bot, chat_id, "📋 Recent tasks:\n" + "\n".join(f"#{row['id']} [{row['status']}] {row['goal'][:50]}" for row in rows)
                          if rows else "No tasks yet. Use /tugas <goal>.")
        elif command == "status":
            sid = self.conversations.sessions.get(session_key(bot, chat_id, persona))
            info = self.backend.session_info(sid) if sid else {}
            model = info.get("model") or "default"
            self.send(bot, chat_id, f"📊 STATUS\nUptime: {int(time.time() - self.started)}s\nModel: {model}\n"
                      f"Agent: {info.get('agent', 'default')}\nPersonas: {len(self.personas.list())}")
        elif command == "reset":
            self.conversations.reset(bot, chat_id, persona)
            self.send(bot, chat_id, f"🆕 '{persona['nama']}' memory wiped!")
        elif command == "new":
            self.conversations.fresh(bot, chat_id, persona)
            self.send(bot, chat_id, "♻️ Fresh context prepared with a handoff summary. Old session archived.")
        elif command in ("job", "jobs", "result", "resolve"):
            self.send(bot, chat_id, "Use job controls in your private chat with the bot.")
        else:
            self.send(bot, chat_id, f"❓ /{command} is unknown — type /help")

    def callback(self, bot, callback):
        def answer(text):
            self.call(bot["token"], "answerCallbackQuery", {"callback_query_id": callback["id"], "text": text[:200]})
        if not self.authorize(callback.get("from") or {}):
            answer("Only the owner can control this bot.")
            return
        message = callback.get("message") or {}
        chat_id, message_id = (message.get("chat") or {}).get("id"), message.get("message_id")
        if chat_id is None or message_id is None:
            answer("Message is unavailable.")
            return
        data = callback.get("data", "")
        try:
            menu = {"c-md": "model", "c-persona": "agent", "c-jd": "jadwal", "c-ps": "persona", "c-st": "status", "c-rs": "reset"}
            if data in menu:
                self.handle(bot, chat_id, menu[data])
                answer("Done")
            elif data.startswith(("ap-y-", "ap-n-")):
                ok, text = self.approvals.decide(data[5:], "once" if data.startswith("ap-y-") else "reject",
                    callback["from"]["id"], bot["token"], chat_id)
                answer(text)
                if ok:
                    self.edit(bot, chat_id, message_id, text, buttons=[])
            elif data in ("md-list", "ag-list"):
                self.panel(bot, chat_id, "model" if data == "md-list" else "agent", message_id)
                answer("List refreshed")
            elif data.startswith(("md-", "ag-")):
                models = data.startswith("md-")
                rows = self.cache.get(bot_id(bot), {}).get("models" if models else "agents", [])
                index = int(data[3:])
                if not 0 <= index < len(rows):
                    answer("Selection expired; reopen the list.")
                    return
                sid = self.conversations.get(bot, chat_id)
                if models:
                    self.backend.set_model(sid, rows[index]["providerID"], rows[index]["modelID"])
                else:
                    self.backend.set_agent(sid, rows[index])
                answer("Selection applied")
                self.edit(bot, chat_id, message_id, "✅ Selection applied.")
            elif data.startswith(("pv-y-", "pv-n-")):
                ok = self.provision.decide(bot, chat_id, message_id, data[3], data[5:], callback["from"]["id"])
                answer("Decision processed" if ok else "Request expired or belongs to another owner.")
            elif data.startswith("st-"):
                if not self.approvals.owns(data[3:], bot["token"], chat_id):
                    answer("Session does not belong to this conversation.")
                    return
                self.backend.interrupt(data[3:])
                answer("Stopping…")
            elif data.startswith("jd-"):
                identity = int(data[3:])
                row = next((row for row in self.store.list_schedules() if row["id"] == identity), None)
                if not row or str(row["bot_id"]) != bot_id(bot) or int(row["chat_id"]) != int(chat_id):
                    answer("Schedule no longer exists in this conversation.")
                    return
                self.store.delete_schedule(identity)
                self.panel(bot, chat_id, "jadwal", message_id)
            elif data.startswith("jx-"):
                identity = int(data[3:])
                job = self.jobs.get(identity)
                if (not job or job["channel"] != "telegram" or job["account_id"] != bot_id(bot)
                        or job["conversation_id"] != str(chat_id)):
                    answer("Job does not belong to this conversation.")
                    return
                if job["status"] == "queued" and self.jobs.cancel_queued(identity):
                    answer(f"Job #{identity} cancelled")
                    self.edit(bot, chat_id, message_id, f"🚫 Job #{identity} cancelled before it ran.")
                else:
                    answer("The job already started; it can no longer be cancelled here.")
                answer("Schedule deleted")
            else:
                answer("Control expired; reopen /help.")
        except Exception as error:
            self.log("Callback failed", type(error).__name__)
            answer("Action failed; reopen the command or inspect /jobs.")
