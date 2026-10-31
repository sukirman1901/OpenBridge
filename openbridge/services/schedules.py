"""Legacy schedule import, display, and natural-language reminder parsing."""
from datetime import datetime, timedelta, timezone
import re
import time

from openbridge.infrastructure.files import read_json, write_json
from runtime_policy import schedule_owner

WIB = timezone(timedelta(hours=7))


def one_shot(days, clock):
    now = datetime.now(WIB)
    hour, minute = map(int, clock.split(":"))
    target = (now + timedelta(days=days)).replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    offset = (target.date() - now.date()).days
    label = "today" if offset == 0 else "tomorrow" if offset == 1 else f"in {offset} days"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(target.timestamp())), target.strftime("%H:%M %d/%m"), label


class Schedules:
    def __init__(self, home, store, config, accounts, log):
        self.path = home / "cron.json"
        self.store, self.config, self.accounts, self.log = store, config, accounts, log

    def sync(self):
        try:
            source = read_json(self.path, [])
        except (OSError, ValueError):
            return
        if not isinstance(source, list):
            return
        known = {(row["bot_id"], int(row["chat_id"]), row["expr"], row["prompt"])
                 for row in self.store.list_schedules()}
        retained = []
        for entry in [*self.config.get("cron", []), *source]:
            try:
                if not isinstance(entry, dict) or not entry.get("aktif", True):
                    if entry in source:
                        retained.append(entry)
                    continue
                owner = schedule_owner(entry, self.accounts())
                expression = str(entry.get("jam", "")).strip()
                if owner is None or not expression:
                    if entry in source:
                        retained.append(entry)
                    continue
                key = owner, int(entry["chat_id"]), expression, entry.get("prompt") or "Send a short automatic message."
                if key not in known:
                    self.store.add_schedule(key[0], key[1], key[2], "cron", key[3])
                    known.add(key)
            except (ValueError, TypeError, KeyError):
                if entry in source:
                    retained.append(entry)
        if source != retained:
            write_json(self.path, retained)

    def list(self):
        return [{"id": row["id"], "bot_id": row["bot_id"], "chat_id": int(row["chat_id"]),
                 "jam": row["expr"], "prompt": row["prompt"], "last_fired": row["last_fired"]}
                for row in self.store.list_schedules(active_only=True)]

    def interpret(self, account_id, conversation_id, text, addressed):
        if re.search(r"batalkan jadwal", text, re.I):
            for row in self.store.list_schedules():
                if str(row["bot_id"]) == account_id and int(row["chat_id"]) == int(conversation_id):
                    self.store.delete_schedule(row["id"])
            return "🗑️ All schedules in this chat are cancelled."
        reminder = re.search(r"(?:ingetin|ingatkan|remind\w*)\s*(?:aku|kami|saya)?\s*(besok|lusa)?\s*[a-z ]{0,10}?(?:jam|pukul)\s*(\d{1,2})(?:[.:](\d{2}))?", text, re.I)
        if reminder and addressed:
            days = {"besok": 1, "lusa": 2}.get(reminder.group(1) or "", 0)
            clock = f"{int(reminder.group(2)) % 24:02d}:{(reminder.group(3) or '00')[:2].zfill(2)}"
            expression, display, when = one_shot(days, clock)
            self.store.add_schedule(account_id, int(conversation_id), expression, "once", f"Send a short reminder using this context: {text[:200]}")
            return f"✅ I'll remind you at {display} WIB ({when})."
        daily = re.search(r"jadwalkan.*?(?:jam|pukul)\s*(\d{1,2})(?:[.:](\d{2}))?", text, re.I)
        if daily and addressed:
            clock = f"{int(daily.group(1)) % 24:02d}:{(daily.group(2) or '00')[:2].zfill(2)}"
            self.store.add_schedule(account_id, int(conversation_id), clock, "cron", "Send a short warm greeting.")
            return f"✅ I'll greet here every day at {clock} WIB."
        return None
