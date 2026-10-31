"""Legacy schedule import, display, and natural-language routine management."""
from datetime import datetime, timedelta, timezone
import re
import time

from openbridge.infrastructure.files import read_json, write_json
from openbridge.services.routines import (WIB, parse_creation, parse_directive,
                                          format_expr)
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

    def list(self, active_only=True):
        return [{"id": row["id"], "bot_id": row["bot_id"], "chat_id": int(row["chat_id"]),
                 "jam": row["expr"], "prompt": row["prompt"], "last_fired": row["last_fired"],
                 "kind": row["kind"], "active": bool(row["active"])}
                for row in self.store.list_schedules(active_only=active_only)]

    def interpret(self, account_id, conversation_id, text, addressed):
        """Handle one message as either a routine op, a new routine, or None."""
        directive = parse_directive(text)
        if directive:
            return self._apply_directive(account_id, conversation_id, directive)
        spec = parse_creation(text) if addressed else None
        if spec is None:
            return None
        prompt = f"Send a short reminder using this context: {text[:200]}"
        schedule_id = self.store.add_schedule(account_id, int(conversation_id), spec["expr"], spec["kind"], prompt)
        detail = f"#{schedule_id}" if spec["kind"] == "once" else f"#{schedule_id}"
        if spec["kind"] == "once":
            return f"✅ Routine {detail} set for {spec['display']} WIB ({spec['label']}) — edit, pause, or cancel it anytime."
        return f"✅ Routine {detail} saved: {spec['display']} — edit, pause, or cancel it anytime."

    def _apply_directive(self, account_id, conversation_id, directive):
        operation = directive["op"]
        if operation == "ambiguous_cancel":
            return ("⏳ Which routine? Use /jadwal to see the numbered list, then say e.g. "
                    "'batalkan jadwal 2'. Say 'batalkan semua jadwal' to clear every routine in this chat.")
        if operation == "cancel_all":
            removed = 0
            for row in self.store.list_schedules():
                if str(row["bot_id"]) == account_id and int(row["chat_id"]) == int(conversation_id):
                    removed += 1 if self.store.delete_schedule(row["id"]) else 0
            return f"🗑️ All {removed} routine(s) in this chat are cancelled."
        schedule_id = directive["id"]
        row = self.store.get_schedule(schedule_id)
        if row is None or str(row["bot_id"]) != account_id or int(row["chat_id"]) != int(conversation_id):
            return f"❓ No routine #{schedule_id} in this chat. /jadwal shows the numbered list."
        if operation == "delete":
            self.store.delete_schedule(schedule_id)
            return f"🗑️ Routine #{schedule_id} ({format_expr(row)}) deleted."
        if operation == "pause":
            self.store.set_schedule_active(schedule_id, False)
            return f"⏸ Routine #{schedule_id} ({format_expr(row)}) paused — resume with 'lanjutkan jadwal {schedule_id}'."
        if operation == "resume":
            self.store.set_schedule_active(schedule_id, True)
            return f"▶️ Routine #{schedule_id} ({format_expr(row)}) resumed."
        # edit
        delta = int(directive["hour"]) % 24, int(directive["minute"]) % 60
        if row["kind"] == "once":
            try:
                current = datetime.strptime(str(row["expr"]), "%Y-%m-%d %H:%M")
            except ValueError:
                return f"⚠️ Routine #{schedule_id} cannot be edited as a time expression."
            if directive["offset"]:
                # "besok"/"lusa": recompute the date from today (WIB) like new
                # one-shots, then convert to the stored UTC-naive convention.
                now_wib = datetime.now(WIB)
                target = (now_wib + timedelta(days=int(directive["offset"]))).replace(
                    hour=delta[0], minute=delta[1], second=0, microsecond=0)
                if target <= now_wib:
                    target += timedelta(days=1)
                expression = time.strftime("%Y-%m-%d %H:%M", time.gmtime(target.timestamp()))
            else:
                current = current.replace(hour=delta[0], minute=delta[1])
                current = current.replace(tzinfo=timezone.utc)
                if current <= datetime.now(timezone.utc):
                    current += timedelta(days=1)
                expression = current.strftime("%Y-%m-%d %H:%M")
        elif row["kind"] == "cron":
            if re.fullmatch(r"(\d{1,2}) (\d{1,2}) \* \* (\d)", str(row["expr"]).strip()):
                parts = str(row["expr"]).split()
                expression = f"{delta[1]:02d} {delta[0]:02d} * * {parts[4]}"
            elif re.fullmatch(r"\d{1,2}:\d{2}", str(row["expr"]).strip()):
                expression = f"{delta[0]:02d}:{delta[1]:02d}"
            else:
                return f"⚠️ Routine #{schedule_id} has a server-managed expression; delete and recreate it instead."
        else:
            return f"⚠️ Interval routines aren't editable; delete routine #{schedule_id} and recreate it."
        self.store.update_schedule_expr(schedule_id, expression)
        refreshed = self.store.get_schedule(schedule_id)
        return f"✏️ Routine #{schedule_id} updated to {format_expr(refreshed)}."
