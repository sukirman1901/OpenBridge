"""Conversation sessions and prompts, isolated from channel wire formats."""
import json
import threading
import uuid

from openbridge.domain.messages import ConversationKey
from openbridge.domain.errors import RetryLater, TaskFailed
from openbridge.services.briefing import conversation_briefing
from openbridge.services.sessions import ConversationSessions, BackgroundSessions
from runtime_policy import bot_id


def conversation_key(bot, chat_id):
    return ConversationKey("telegram", bot_id(bot), str(chat_id))


def session_key(bot, chat_id, persona, mode="grup"):
    return conversation_key(bot, chat_id), persona["nama"], mode


class Conversations:
    def __init__(self, home, config, backend, runtime, personas, approvals, context, meter, rate, send, checkpoint, log,
                 memories=None):
        self.home, self.config, self.backend, self.runtime = home, config, backend, runtime
        self.personas, self.approvals, self.context, self.meter, self.rate = personas, approvals, context, meter, rate
        self.send, self.checkpoint, self.log = send, checkpoint, log
        self.memories = memories
        self.sessions, self.turns = {}, {}
        self.lock = threading.Lock()

    def permissions(self):
        with (self.home / "opencode.jsonc").open() as handle:
            rules = json.load(handle).get("permissions")
        if not isinstance(rules, list) or not rules:
            raise RuntimeError("Native V2 permission rules are required")
        return rules

    def get(self, bot, chat_id, persona=None, mode="grup", handoff=""):
        persona = persona or {"nama": bot["nama"], "persona": bot.get("persona", "")}
        def briefing():
            return conversation_briefing(name=persona["nama"], persona=persona["persona"], account_id=bot_id(bot),
                chat_id=chat_id, memory_file=self.personas.memory(persona["nama"]),
                schedule_file=self.home / "cron.json", persona_file=self.home / "personas.json",
                provision_file=self.home / "provision.json", output_dir=self.home / "hasil", mode=mode, handoff=handoff,
                siblings=[row["nama"] for row in self.personas.list() if row["nama"] != persona["nama"]],
                memories=self.memories(bot_id(bot), chat_id) if self.memories else ())
        service = ConversationSessions(self.sessions, self.lock,
            lambda title: self.backend.open_session(title, str(self.home), self.permissions()),
            lambda sid, text, timeout: self.backend.run_prompt(sid, text, timeout=timeout),
            lambda sid: self.approvals.register(sid, bot, chat_id), self.checkpoint, self.log)
        return service.get(session_key(bot, chat_id, persona, mode),
                           f"telegram-{bot_id(bot)}-{persona['nama']}-{chat_id}-{mode}", briefing)

    def reset(self, bot, chat_id, persona):
        key = session_key(bot, chat_id, persona)
        if key in self.sessions:
            self.backend.delete_session(self.sessions[key])
            del self.sessions[key]
        return self.get(bot, chat_id, persona)

    def fresh(self, bot, chat_id, persona):
        key = session_key(bot, chat_id, persona)
        old = self.sessions.get(key)
        summary = ""
        if old:
            self.checkpoint(old)
            self.backend.compact_session(old)
            summary = self.backend.run_prompt(old, "Write a handoff summary: topics, decisions, open work. Max 120 words.", timeout=90)
            # Keep the original lookup title for the new session, not the archived one.
            self.backend.rename_session(old, f"archived-{old}-{uuid.uuid4().hex[:8]}")
            del self.sessions[key]
        return self.get(bot, chat_id, persona, handoff=summary)

    def answer(self, bot, chat_id, sender, text, kind):
        identity = bot_id(bot)
        if self.meter.over_budget(identity):
            self.send(bot, chat_id, "⚠️ Today's agent budget is exhausted.")
            return
        if self.rate and not self.rate.allow(identity):
            self.send(bot, chat_id, "⚠️ The agent's per-minute limit is reached. Please try again shortly.")
            return
        sid = self.get(bot, chat_id)
        self.checkpoint(sid)
        context = self.context.format_context(chat_id, self.config.get("trigger", {}).get("konteks_n", 15))
        answer = self.backend.run_prompt(sid, f"[Recent conversation context]:\n{context}\n\n[Message from {sender}]: {text}")
        self.context.append(chat_id, "bot", answer, name=bot["nama"])
        self.send(bot, chat_id, answer)
        self.meter.extract_and_record(identity, self.backend.session_info(sid))
        key = conversation_key(bot, chat_id)
        self.turns[key] = self.turns.get(key, 0) + 1
        if kind == "private" and self.turns[key] >= int(self.config.get("compact_per_turns", 120)):
            self.backend.compact_session(sid)
            self.turns[key] = 0

    def background_session(self, job, bot):
        return BackgroundSessions(self.backend, str(self.home), self.permissions,
            lambda sid: self.approvals.register(sid, bot, job["conversation_id"]), self.checkpoint).get(job)

    def background_model(self, bot, sid, prompt, timeout):
        if self.meter.over_budget(bot_id(bot)):
            job = self.runtime.current_job()
            self.send(bot, job["conversation_id"], "⚠️ Background work stopped: today's agent budget is exhausted.")
            raise TaskFailed("Daily budget exhausted")
        if self.rate and not self.rate.allow(bot_id(bot)):
            raise RetryLater(15)
        context = (f"You are {bot['nama']}. Persona: {bot.get('persona', '')}\n"
                   "Respect approvals; never access bridge credentials. Verify work; deliverables go under hasil/.\n")
        answer = self.backend.run_prompt(sid, context + prompt, timeout=timeout)
        try:
            self.meter.extract_and_record(bot_id(bot), self.backend.session_info(sid))
        except Exception as error:
            self.log("Usage accounting failed", type(error).__name__)
        return answer
