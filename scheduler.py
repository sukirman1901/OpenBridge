#!/usr/bin/env python3
"""
scheduler.py — parser & matcher ekspresi jadwal untuk bridge groupbot
┌ Dukungan format ┐
- Legacy bridge: "21:00"  → harian jam 21:00
- Legacy bridge: "*/N"    → interval tiap N menit
- Cron standar 5 field  : "m h dom mon dow" (bisa berspasi, mis. "*/30 * * * *")
- Ekspresi invalid      → raise ValueError dengan pesan jelas
┌ Catatan zona waktu ┐
Semua pencocokan berjalan pada offset konstan UTC+7
(timezone(timedelta(hours=7))) supaya tidak bergantung tzdata yang
mungkin tidak tersedia di proot. Konstanta meniadakan efek DST.
Murni fungsi: tanpa state global, aman untuk dipanggil dari thread mana pun.
"""
import re
from datetime import datetime, timedelta

# ── Konstanta zona ─────────────────────────────────────────────
TZ_OFFSET = timedelta(hours=7)          # offset konstan UTC+7 (anti-DST)
EPOCH = datetime(1970, 1, 1)
WADAH_HARI = 1440                      # menit per hari (untuk horizon)

# batas tiap field cron: (nama, lo, hi)
_FIELD_BATAS = [("minute", 0, 59), ("hour", 0, 23),
                ("dom", 1, 31), ("mon", 1, 12), ("dow", 0, 7)]


# ── Alat bantu waktu (tanpa tzdata) ────────────────────────────
def _ke_utc(dt):
    """Normalisasi masukan (epoch detik / datetime) → datetime naive UTC.
    datetime naive diasumsikan sudah UTC (konvensi utcnow())."""
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            return dt.replace(tzinfo=None) - dt.utcoffset()
        return dt
    dt = float(dt)
    return EPOCH + timedelta(seconds=dt)


def _epoch_utc(dt_utc):
    """datetime naive UTC → epoch detik (tanpa menyentuh timezone lokal)."""
    return (dt_utc - EPOCH).total_seconds()


def _ke_lokal(dt):
    """Masukan apa pun → (dt_naive_lokal_utc7, dt_naive_utc)."""
    u = _ke_utc(dt)
    return u + TZ_OFFSET, u


# ── Parser field cron ──────────────────────────────────────────
def _parse_atom(tok, lo, hi, nama):
    """Satu item field: angka, rentang a-b, keduanya boleh berlangkah /N."""
    langkah = 1
    if "/" in tok:
        tok, s = tok.split("/", 1)
        try:
            langkah = int(s)
        except ValueError:
            raise ValueError(f"field '{nama}': langkah '{s}' bukan angka")
        if langkah < 1:
            raise ValueError(f"field '{nama}': langkah {langkah} harus >= 1")
    if tok == "*" or tok == "":
        if langkah == 1:
            return set(range(lo, hi + 1))
        return set(range(lo, hi + 1, langkah))
    if "-" in tok and tok.count("-") == 1:
        a, b = tok.split("-", 1)
    else:
        a = b = tok                  # angka tunggal = rentang sepanjang 1
    try:
        a, b = int(a), int(b)
    except (TypeError, ValueError):
        raise ValueError(f"field '{nama}': nilai '{tok}' bukan angka")
    if not (lo <= a <= hi) or not (lo <= b <= hi) or b < a:
        raise ValueError(f"field '{nama}': rentang {a}-{b} di luar batas {lo}-{hi}")
    return set(x for x in range(a, b + 1) if (x - a) % langkah == 0)


def _parse_field(tok, lo, hi, nama):
    """Daftar koma atom, atau '*' / '*/N'."""
    tok = (tok or "").strip()
    if not tok:
        raise ValueError(f"field '{nama}': kosong")
    hasil = set()
    for bagian in tok.split(","):
        hasil |= _parse_atom(bagian.strip(), lo, hi, nama)
    return hasil


# ── API utama ──────────────────────────────────────────────────
def parse(expr, tz="Asia/Jakarta"):
    """Ekspresi jadwal → dict {kind, fields, minute, hour, dom, mon, dow}.

    kind: "daily" | "interval" | "cron". Set minute/hour/dom/mon/dow berisi
    nilai-nilai yang dianggap cocok pada zona UTC+7. Invalid → ValueError.
    """
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError("ekspresi jadwal kosong")
    teks = expr.strip()

    # legacy 1: "HH:MM" → jadwal harian
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", teks)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            raise ValueError(f"'{teks}': jam {hh:02d}:{mm:02d} di luar 00:00-23:59")
        return {"kind": "daily", "fields": {"hour": hh, "minute": mm},
                "minute": {mm}, "hour": {hh},
                "dom": set(range(1, 32)), "mon": set(range(1, 13)),
                "dow": set(range(0, 7))}

    # legacy 2: "*/N" → interval tiap N menit
    m = re.fullmatch(r"\*/(\d+)", teks)
    if m:
        n = int(m.group(1))
        if not 1 <= n <= 59:
            raise ValueError(f"'{teks}': interval N harus 1-59 menit")
        menit = set(x for x in range(0, 60) if x % n == 0)
        return {"kind": "interval", "fields": {"every": n},
                "minute": menit, "hour": set(range(0, 24)),
                "dom": set(range(1, 32)), "mon": set(range(1, 13)),
                "dow": set(range(0, 7))}

    # cron standar: 5 field dipisah spasi
    bagian = teks.split()
    if len(bagian) != 5:
        raise ValueError(
            f"'{teks}': bukan format dikenal — harap 'HH:MM', '*/N', "
            "atau cron 5 field 'm h dom mon dow' (dapat {len(bagian)} field)")
    hasil = {"kind": "cron", "fields": dict(zip([n for n, _, _ in _FIELD_BATAS], bagian))}
    for tok, (nama, lo, hi) in zip(bagian, _FIELD_BATAS):
        hasil[nama] = _parse_field(tok, lo, hi, nama)
    hasil["dom"] -= {0}
    hasil["mon"] &= set(range(1, 13))
    # 7 pada dow setara 0 (Minggu, konvensi cron)
    if 7 in hasil["dow"]:                # 7 = setara Minggu (0) di cron standar
        hasil["dow"] = (hasil["dow"] - {7}) | {0}
    return hasil


def matches(expr, dt=None, tz="Asia/Jakarta"):
    """True jika momen dt (epoch detik / datetime, default utcnow) cocok expr."""
    j = parse(expr, tz)
    _, u = _ke_lokal(dt if dt is not None else datetime.utcnow())
    lokal = u + TZ_OFFSET
    if lokal.minute not in j["minute"] or lokal.hour not in j["hour"]:
        return False
    if lokal.month not in j["mon"]:
        return False
    dom_penuh = len(j["dom"]) == 31
    dow_penuh = len(j["dow"]) == 7
    if dom_penuh and dow_penuh:
        return True
    dom_kena = lokal.day in j["dom"]
    dow_kena = (lokal.weekday() + 1) % 7 in j["dow"]   # Sen=1..Min=0 (cron)
    if dom_penuh:
        return dow_kena
    if dow_penuh:
        return dom_kena
    # keduanya dibatasi → semantik cron: cukup salah satu cocok
    return dom_kena or dow_kena


def next_run(expr, after=None, tz="Asia/Jakarta", horizon_days=370):
    """Epoch detik kemunculan berikutnya setelah `after` (None: sekarang).
    None kalau tidak ada dalam horizon (contoh: 31 Feb)."""
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
    """Cocokkan dom/dow dengan semantik cron (dua-dua dibatasi → OR)."""
    dom_penuh = len(j["dom"]) == 31 or 0 in j["dom"]
    dow_penuh = len(j["dow"]) == 7
    dom_kena = lokal.day in j["dom"]
    dow_kena = (lokal.weekday() + 1) % 7 in j["dow"]
    if dom_penuh and dow_penuh:
        return True
    if dom_penuh:
        return dow_kena
    if dow_penuh:
        return dom_kena
    return dom_kena or dow_kena
