#!/usr/bin/env python3
"""
scheduler.py — schedule expression parser & matcher for the groupbot bridge
┌ Supported formats ┐
- Legacy bridge: "21:00"  → daily at 21:00
- Legacy bridge: "*/N"    → interval every N minutes
- Standard 5-field cron  : "m h dom mon dow" (spaces allowed, e.g. "*/30 * * * *")
- Invalid expressions    → raise ValueError with a clear message
┌ Timezone notes ┐
All matching runs on a constant UTC+7 offset
(timezone(timedelta(hours=7)) so it does not depend on tzdata, which may
be unavailable in proot. The constant neutralizes any DST effect.
Pure functions: no global state, safe to call from any thread.
"""
import re
from datetime import datetime, timedelta

# ── Timezone constants ─────────────────────────────────────────
TZ_OFFSET = timedelta(hours=7)          # constant UTC+7 offset (DST-immune)
EPOCH = datetime(1970, 1, 1)
DAY_BUCKETS = 1440                      # minutes per day (for the horizon)

# per-field cron bounds: (name, lo, hi)
_FIELD_BATAS = [("minute", 0, 59), ("hour", 0, 23),
                ("dom", 1, 31), ("mon", 1, 12), ("dow", 0, 7)]


# ── Time helpers (no tzdata) ───────────────────────────────────
def _ke_utc(dt):
    """Normalize input (epoch seconds / datetime) → naive UTC datetime.
    Naive datetimes are assumed to already be UTC (utcnow() convention)."""
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            return dt.replace(tzinfo=None) - dt.utcoffset()
        return dt
    dt = float(dt)
    return EPOCH + timedelta(seconds=dt)


def _epoch_utc(dt_utc):
    """Naive UTC datetime → epoch seconds (without touching the local timezone)."""
    return (dt_utc - EPOCH).total_seconds()


def _ke_lokal(dt):
    """Any input → (naive UTC+7 local dt, naive UTC dt)."""
    u = _ke_utc(dt)
    return u + TZ_OFFSET, u


# ── Cron field parser ──────────────────────────────────────────
def _parse_atom(tok, lo, hi, nama):
    """One field item: a number, an a-b range; both may carry a /N step."""
    langkah = 1
    if "/" in tok:
        tok, s = tok.split("/", 1)
        try:
            langkah = int(s)
        except ValueError:
            raise ValueError(f"field '{nama}': step '{s}' is not a number")
        if langkah < 1:
            raise ValueError(f"field '{nama}': step {langkah} must be >= 1")
    if tok == "*" or tok == "":
        if langkah == 1:
            return set(range(lo, hi + 1))
        return set(range(lo, hi + 1, langkah))
    if "-" in tok and tok.count("-") == 1:
        a, b = tok.split("-", 1)
    else:
        a = b = tok                  # a single number = a range of length 1
    try:
        a, b = int(a), int(b)
    except (TypeError, ValueError):
        raise ValueError(f"field '{nama}': value '{tok}' is not a number")
    if not (lo <= a <= hi) or not (lo <= b <= hi) or b < a:
        raise ValueError(f"field '{nama}': range {a}-{b} outside bounds {lo}-{hi}")
    return set(x for x in range(a, b + 1) if (x - a) % langkah == 0)


def _parse_field(tok, lo, hi, nama):
    """Comma-separated list of atoms, or '*' / '*/N'."""
    tok = (tok or "").strip()
    if not tok:
        raise ValueError(f"field '{nama}': empty")
    hasil = set()
    for bagian in tok.split(","):
        hasil |= _parse_atom(bagian.strip(), lo, hi, nama)
    return hasil


# ── Main API ──────────────────────────────────────────────────
def parse(expr, tz="Asia/Jakarta"):
    """Schedule expression → dict {kind, fields, minute, hour, dom, mon, dow}.

    kind: "daily" | "interval" | "cron". The minute/hour/dom/mon/dow sets hold
    the values considered matching in the UTC+7 zone. Invalid → ValueError.
    """
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError("empty schedule expression")
    teks = expr.strip()

    # legacy 1: "HH:MM" → daily schedule
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", teks)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            raise ValueError(f"'{teks}': time {hh:02d}:{mm:02d} outside 00:00-23:59")
        return {"kind": "daily", "fields": {"hour": hh, "minute": mm},
                "minute": {mm}, "hour": {hh},
                "dom": set(range(1, 32)), "mon": set(range(1, 13)),
                "dow": set(range(0, 7))}

    # legacy 2: "*/N" → interval every N minutes
    m = re.fullmatch(r"\*/(\d+)", teks)
    if m:
        n = int(m.group(1))
        if not 1 <= n <= 59:
            raise ValueError(f"'{teks}': interval N must be 1-59 minutes")
        menit = set(x for x in range(0, 60) if x % n == 0)
        return {"kind": "interval", "fields": {"every": n},
                "minute": menit, "hour": set(range(0, 24)),
                "dom": set(range(1, 32)), "mon": set(range(1, 13)),
                "dow": set(range(0, 7))}

    # standard cron: 5 space-separated fields
    bagian = teks.split()
    if len(bagian) != 5:
        raise ValueError(
            f"'{teks}': unrecognized format — expected 'HH:MM', '*/N', "
            "or 5-field cron 'm h dom mon dow' (got {len(bagian)} fields)")
    hasil = {"kind": "cron", "fields": dict(zip([n for n, _, _ in _FIELD_BATAS], bagian))}
    for tok, (nama, lo, hi) in zip(bagian, _FIELD_BATAS):
        hasil[nama] = _parse_field(tok, lo, hi, nama)
    hasil["dom"] -= {0}
    hasil["mon"] &= set(range(1, 13))
    # 7 on dow is equivalent to 0 (Sunday, cron convention)
    if 7 in hasil["dow"]:                # 7 = equivalent to Sunday (0) in standard cron
        hasil["dow"] = (hasil["dow"] - {7}) | {0}
    return hasil


def matches(expr, dt=None, tz="Asia/Jakarta"):
    """True if the moment dt (epoch seconds / datetime, default utcnow) matches expr."""
    j = parse(expr, tz)
    _, u = _ke_lokal(dt if dt is not None else datetime.utcnow())
    lokal = u + TZ_OFFSET
    if lokal.minute not in j["minute"] or lokal.hour not in j["hour"]:
        return False
    if lokal.month not in j["mon"]:
        return False
    dom_full = len(j["dom"]) == 31
    dow_full = len(j["dow"]) == 7
    if dom_full and dow_full:
        return True
    dom_kena = lokal.day in j["dom"]
    dow_kena = (lokal.weekday() + 1) % 7 in j["dow"]   # Mon=1..Sun=0 (cron)
    if dom_full:
        return dow_kena
    if dow_full:
        return dom_kena
    # both restricted → cron semantics: matching either one suffices
    return dom_kena or dow_kena


def next_run(expr, after=None, tz="Asia/Jakarta", horizon_days=370):
    """Epoch seconds of the next occurrence after `after` (None: now).
    None when there is none within the horizon (e.g. Feb 31)."""
    j = parse(expr, tz)
    lokal, _ = _ke_lokal(after if after is not None else datetime.utcnow())
    mulai = lokal.replace(second=0, microsecond=0) + timedelta(minutes=1)
    batas = mulai + timedelta(days=float(horizon_days))
    kandidat = mulai
    langkah = timedelta(minutes=1)
    while kandidat < batas:
        if (kandidat.minute in j["minute"] and kandidat.hour in j["hour"]
                and kandidat.month in j["mon"]
                and _kena_hari(j, kandidat)):
            return _epoch_utc(kandidat - TZ_OFFSET)
        kandidat += langkah
    return None


def _kena_hari(j, lokal):
    """Match dom/dow with cron semantics (both restricted → OR)."""
    dom_full = len(j["dom"]) == 31 or 0 in j["dom"]
    dow_full = len(j["dow"]) == 7
    dom_kena = lokal.day in j["dom"]
    dow_kena = (lokal.weekday() + 1) % 7 in j["dow"]
    if dom_full and dow_full:
        return True
    if dom_full:
        return dow_kena
    if dow_full:
        return dom_kena
    return dom_kena or dow_kena
