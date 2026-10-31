"""Conversation sessions and prompts, isolated from channel wire formats."""
import json
import threading
import uuid

from openbridge.domain.messages import ConversationKey
from openbridge.domain.errors import RetryLater, TaskFailed
from openbridge.services.briefing import conversation_briefing
from openbridge.services.background import parse_file_outputs
from openbridge.services.sessions import ConversationSessions, BackgroundSessions
from openbridge.services.untrusted import framed
from runtime_policy import bot_id


def conversation_key(bot, chat_id):
    return ConversationKey("telegram", bot_id(bot), str(chat_id))


def session_key(bot, chat_id, persona, mode="grup"):
    return conversation_key(bot, chat_id), persona["nama"], mode


class Conversations:
    def __init__(self, home, config, backend, runtime, personas, approvals, context, meter, rate, send, checkpoint, log,
                 memories=None, send_file=None, prompt=None, prepared=None, mark_prepared=None, usage=None):
        self.home, self.config, self.backend, self.runtime = home, config, backend, runtime
        self.personas, self.approvals, self.context, self.meter, self.rate = personas, approvals, context, meter, rate
        self.send, self.checkpoint, self.log = send, checkpoint, log
        self.memories = memories
        self.send_file = send_file
        self.prompt = prompt
        self.prepared, self.mark_prepared, self.usage = prepared, mark_prepared, usage
        self.sessions, self.turns = {}, {}
        self.lock = threading.Lock()
        self.session_locks = {}

    def _run_prompt(self, sid, text, timeout=120, purpose='reply'):
        if self.prompt:
            return self.prompt(sid, text, timeout=timeout, purpose=purpose)
        return self.backend.run_prompt(sid, text, timeout=timeout)

    def permissions(self):
        with (self.home / "opencode.jsonc").open() as handle:
            rules = json.load(handle).get("permissions")
        if not isinstance(rules, list) or not rules:
            raise RuntimeError("Native V2 permission rules are required")
        return rules

    def get(self, bot, chat_id, persona=None, mode="grup", handoff=""):
        persona = persona or {"nama": bot["nama"], "persona": bot.get("persona", "")}
        key = session_key(bot, chat_id, persona, mode)
        with self.lock:
            lock = self.session_locks.setdefault(key, threading.Lock())
        def briefing(summary=handoff):
            return conversation_briefing(name=persona["nama"], persona=persona["persona"], account_id=bot_id(bot),
                chat_id=chat_id, memory_file=self.personas.memory(persona["nama"]),
                schedule_file=self.home / "cron.json", persona_file=self.home / "personas.json",
                provision_file=self.home / "provision.json", output_dir=self.home / "hasil", mode=mode, handoff=summary,
                siblings=[row["nama"] for row in self.personas.list() if row["nama"] != persona["nama"]],
                memories=self.memories(bot_id(bot), chat_id) if self.memories else ())
        service = ConversationSessions(self.sessions, lock,
            lambda title: self.backend.open_session(title, str(self.home), self.permissions()),
            lambda sid, text, timeout: self._run_prompt(sid, text, timeout=timeout, purpose='briefing'),
            lambda sid: self.approvals.register(sid, bot, chat_id), self.checkpoint, self.log,
            prepared=(lambda sid, text: self.prepared(sid, briefing('') if handoff else text)) if self.prepared else None,
            mark_prepared=(lambda sid, text: self.mark_prepared(sid, briefing('') if handoff else text)) if self.mark_prepared else None)
        return service.get(key,
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
            # The summary itself is the handoff; pre-compacting used to queue
            # async work and race the next prompt, falsely fencing /new.
            summary = self._run_prompt(old, "Write a handoff summary: topics, decisions, open work. Max 120 words.", timeout=90, purpose='handoff')
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
        capabilities = (
            "[Current bridge capabilities — applies to this turn, including existing sessions]\n"
            "You can act on the owner's direct requests in ordinary language. No slash command is required. "
            "Create requested deliverables with your tools under hasil/, verify they exist, then send them "
            "by ending your answer with a separate line: @file hasil/name.ext optional caption. "
            "For an image use @photo hasil/name.png optional caption. The bridge uploads these files to Telegram; "
            "you do not need a separate Telegram tool. Do not claim files are unavailable without trying. "
            "Never fabricate an artifact or claim delivery succeeded before it is confirmed. "
            "Respect approval decisions; if denied, explain the block rather than bypassing it. "
            "Scheduling is available in ordinary chat; ask for a missing time instead of claiming a reminder exists.\n"
        )
        # Owner approval requests may remain pending for five minutes. The
        # backend's short default is not a suitable wall-clock budget for
        # interactive tool work; expiry must still fence unknown execution.
        answer = self._run_prompt(sid, capabilities + f"[Recent conversation context]:\n{context}\n\n{framed('MESSAGE', sender, text)}",
                                         timeout=900)
        self.context.append(chat_id, "bot", answer, name=bot["nama"])
        if self.send_file:
            cleaned, files = parse_file_outputs(answer)
            if cleaned.strip():
                self.send(bot, chat_id, cleaned)
            for item in files:
                self.send_file(identity, chat_id, item['path'], item['caption'], item['operation'])
        else:
            self.send(bot, chat_id, answer)
        try:
            usage = self.usage(sid) if self.usage else self.backend.session_info(sid)
            self.meter.extract_and_record(identity, usage)
        except Exception as error:
            self.log('Usage accounting unavailable', type(error).__name__)
        key = conversation_key(bot, chat_id)
        self.turns[key] = self.turns.get(key, 0) + 1
        if kind == "private" and self.turns[key] >= int(self.config.get("compact_per_turns", 120)):
            if self.prompt:
                self.prompt(sid, '', timeout=600, purpose='compaction')
            else:
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
        job = self.runtime.current_job()
        purpose = 'task_step' if job and job['kind'] == 'task' else job['kind'] if job else 'schedule'
        answer = self._run_prompt(sid, context + prompt, timeout=timeout, purpose=purpose)
        try:
            usage = self.usage(sid) if self.usage else self.backend.session_info(sid)
            self.meter.extract_and_record(bot_id(bot), usage)
        except Exception as error:
            self.log("Usage accounting failed", type(error).__name__)
        return answer
