"""Bridge-level ownership checks, not an OS sandbox."""
import hashlib
import json


def bot_id(bot):
    identity = str(bot.get("bot_id") or bot["token"].split(":", 1)[0])
    if not identity.isdigit():
        raise ValueError("Invalid Telegram bot ID")
    return identity


def authorized(config, user):
    if user.get("is_bot") or not user.get("id"):
        return False
    return int(user["id"]) in {int(x) for x in config.get("owner_ids", [])}


def schedule_owner(entry, bots):
    identity = entry.get("bot_id")
    if identity is None:
        # Unambiguous legacy migration only; never guess with multiple bots.
        return bot_id(bots[0]) if len(bots) == 1 else None
    identity = str(identity)
    return identity if any(bot_id(b) == identity for b in bots) else None


def schedule_key(entry, minute):
    payload = json.dumps(entry, sort_keys=True, ensure_ascii=False)
    return minute, hashlib.sha256(payload.encode()).hexdigest()
