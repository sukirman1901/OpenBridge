"""Natural-language routine directives and creation parsing.

Pure functions: no state, no I/O. The schedules service consumes the returned
directives and applies ownership guards; the creation rules stay here so tests
can cover wording without a database.
"""
import re
import time
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))

WEEKDAYS = {
    "senin": 1, "senen": 1, "monday": 1,
    "selasa": 2, "tuesday": 2,
    "rabu": 3, "wednesday": 3,
    "kamis": 4, "khemis": 4, "thursday": 4,
    "jumat": 5, "jumatan": 5, "friday": 5,
    "sabtu": 6, "saturday": 6,
    "minggu": 0, "sunday": 0,
}
WEEKDAY_NAMES = {value: key.capitalize() for key, value in WEEKDAYS.items()}

_CLOCK = r"(?:jam|pukul|at)?\s*(\d{1,2})(?:[.:](\d{2}))?"
_ID = r"#?\s*(\d+)"

_ROUTINE = r"(?:jadwal|routine|rutinitas|alarm|reminder|pengingat)"
_SEMUA = r"\s*(?:semua|all|kabeh)\b"


def parse_directive(text):
    """Return a routine-management directive dict, or None when not an op phrase.

    Directives carry only the requested operation and any numeric id; the caller
    applies account/chat ownership so a phrase can never touch another chat's
    routines. Editing keeps the current date for one-shots unless the phrase
    says "besok" (tomorrow) or "lusa" (day after tomorrow).
    """
    text = (text or "").strip()
    if not text:
        return None

    if re.search(rf"(?:batalkan|batal|hapus|cancel|delete){_SEMUA}\s*{_ROUTINE}", text, re.I):
        return {"op": "cancel_all"}

    item = re.compile(
        rf"(?:batalkan|batal|hapus|cancel|delete|jeda|pause)\s+{_ROUTINE}\s*{_ID}", re.I)
    match = item.search(text)
    if match:
        return {"op": "delete" if re.search(r"(?:batalkan|hapus|cancel|delete)", match.group(0), re.I)
                else "pause", "id": int(match.group(1))}

    match = re.search(rf"(?:lanjut|lanjutkan|aktifkan|resume)\s+{_ROUTINE}\s*{_ID}", text, re.I)
    if match:
        return {"op": "resume", "id": int(match.group(1))}

    edit = re.search(
        rf"(?:ubah|edit|ganti|perbarui|change|move)\s+{_ROUTINE}\s*{_ID}\s*(?:ke|jadi|menjadi|to|kl)?\s*"
        r"(?:(besok|lusa)\s+)?(?:jam\s*|pukul\s*|at\s*)?(\d{1,2})(?:[.:](\d{2}))?", text, re.I)
    if not edit:
        # Also accept "jadwal 3 ke (besok) (jam) 8" without a leading verb.
        edit = re.search(
            rf"{_ROUTINE}\s*{_ID}\s*(?:ke|jadi|menjadi|to|kl)\s*(?:(besok|lusa)\s+)?(?:jam\s*|pukul\s*|at\s*)?(\d{{1,2}})(?:[.:](\d{{2}}))?", text, re.I)
    if edit:
        return {"op": "edit", "id": int(edit.group(1)),
                "offset": {"besok": 1, "lusa": 2}.get((edit.group(2) or "").lower(), 0),
                "hour": edit.group(3), "minute": edit.group(4) or "00"}

    # Bare "batalkan jadwal" without an id and without "semua": ambiguity must be
    # resolved, never guessed — the old behavior deleted everything on sight.
    if re.search(rf"(?:batalkan|batal|hapus|cancel|delete)\s*{_ROUTINE}|{_ROUTINE}?\s*(?:batalkan|hapus|cancel)\b",
                 text, re.I) and "semua" not in text.lower() and " all " not in f" {text.lower()} ":
        return {"op": "ambiguous_cancel"}

    return None


def parse_creation(text, now=None):
    """Return a creation spec or None when the text asks for a new routine.

    The spec is already normalized: kind 'once' carries a datetime expr,
    'cron' carries "M H * * dow" for weekly, plain "MM H" (HH:MM) for daily.
    """
    text = (text or "").strip()
    if not text:
        return None

    tomorrow_schedule = re.search(
        r"\b(?:jadwal\w*|schedule\w*)\b.*?\b(besok|lusa|tomorrow)\b.*?"
        r"(?:jam|pukul|at)\s*(\d{1,2})(?:[.:](\d{2}))?", text, re.I)
    if tomorrow_schedule:
        days = 2 if tomorrow_schedule.group(1).lower() == 'lusa' else 1
        clock = f"{int(tomorrow_schedule.group(2)) % 24:02d}:{tomorrow_schedule.group(3) or '00'}"
        return _once_spec(days, clock, now)

    reminder = re.search(
        r"(?:ingetin|ingatkan|remind\w*|alarmin)\s*(?:aku|kami|saya|me)?\s*(besok|lusa)?\s*"
        r"(?:\S+\s+)?(?:jam|pukul|at)\s*(\d{1,2})(?:[.:](\d{2}))?", text, re.I)
    if reminder:
        days = {"besok": 1, "lusa": 2}.get((reminder.group(1) or "").lower(), 0)
        clock = f"{int(reminder.group(2)) % 24:02d}:{(reminder.group(3) or '00')[:2].zfill(2)}"
        return _once_spec(days, clock, now)
    once_at = re.search(r"\b(?:ke[jj]apan|nanti)\s*(?:jam|pukul)\s*(\d{1,2})(?:[.:](\d{2}))?", text, re.I)
    if once_at:
        clock = f"{int(once_at.group(1)) % 24:02d}:{(once_at.group(2) or '00')[:2].zfill(2)}"
        return _once_spec(0, clock, now)

    weekly = re.search(
        rf"(?:tiap|setiap|every)\s+(?:hari\s+)?({'|'.join(WEEKDAYS)})\b\s*(?:jam|pukul)?\s*(\d{{1,2}})(?:[.:](\d{{2}}))?", text, re.I)
    if weekly:
        dow = WEEKDAYS[weekly.group(1).lower()]
        hour, minute = int(weekly.group(2)) % 24, (weekly.group(3) or "00")[:2]
        return {"kind": "cron", "expr": f"00 {hour:02d} * * {dow}",
                "display": f"every {WEEKDAY_NAMES[dow]} at {hour:02d}:{minute} WIB"}

    match = re.search(rf"(?:jadwalkan|schedule|rutinkan)\b.*?(?:jam|pukul|at)?\s*(\d{{1,2}})(?:[.:](\d{{2}}))?", text, re.I)
    if match:
        clock = f"{int(match.group(1)) % 24:02d}:{(match.group(2) or '00')[:2].zfill(2)}"
        return {"kind": "cron", "expr": clock, "display": f"every day at {clock} WIB"}
    match = re.search(rf"(?:tiap|setiap|every)\s+hari\b.*?(?:jam|pukul|at)?\s*(\d{{1,2}})(?:[.:](\d{{2}}))?", text, re.I)
    if match:
        clock = f"{int(match.group(1)) % 24:02d}:{(match.group(2) or '00')[:2].zfill(2)}"
        return {"kind": "cron", "expr": clock, "display": f"every day at {clock} WIB"}
    return None


def _once_spec(days, clock, now=None):
    hour, minute = map(int, clock.split(":"))
    now = now or datetime.now(WIB)
    target = (now + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
        days += 1
    label = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
    stored = time.strftime("%Y-%m-%d %H:%M", time.gmtime(target.timestamp()))
    shown = target.strftime("%H:%M %d/%m")
    return {"kind": "once", "expr": stored, "display": shown, "label": label}


def format_expr(row):
    """Human-readable expression for a schedules row (cron/once/interval)."""
    expression = str(row["expr"] or "").strip()
    if row["kind"] == "cron" and re.fullmatch(r"\d{1,2}:\d{2}", expression):
        return expression
    match = re.fullmatch(r"(\d{2}) (\d{2}) \* \* (\d)", expression)
    if match:
        name = WEEKDAY_NAMES.get(int(match.group(3)) % 7, "?")
        return f"{name} {match.group(2)}:{match.group(1)}"
    if row["kind"] == "once":
        return expression
    return expression
