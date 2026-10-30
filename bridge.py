#!/usr/bin/env python3
"""
OpenCode GroupBot Bridge — multi-persona, GrokBot-style
What this file does (for new contributors):
- Free chat with an OpenCode brain (one session per persona/chat)
- Multi-persona: managed through chat ("new persona: Senja, ...") via personas.json
- Slash commands + inline button panels (/model /agent /persona /jadwal /status /reset)
- Natural-language schedules → SQLite (schedules); cron.json is only an inbox
- [SILENT] default mode + initiative loop; daily budget + per-minute rate limiter
- Persona ↔ token split: token≥persona → each persona gets its own real Telegram bot
Run: ./start.sh
"""
import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from context_buffer import ChatBuffer
from usage_meter import UsageMeter
from store import Store

SHUTDOWN = threading.Event()
SHUTDOWN_WAIT = 25
PESAN_BARU = {}          # chat_id → {"bot": …, "ts": epoch} — group activity not yet attended
INISIATIF_TERAKHIR = {}  # chat_id → epoch of the last initiative

def request_shutdown(signum=None, frame=None):
    if SHUTDOWN.is_set():
        return
    log("🛑 shutdown signal received, closing cleanly...")
    CHATBUF.save()
    SHUTDOWN.set()
from runtime_policy import authorized, bot_id, schedule_owner, schedule_key

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
# Path of OpenCode's service.json (written by `opencode serve`, containing url + password).
# Override with the OPENBRIDGE_SERVICE_JSON env var if OpenCode runs on another host/port.
SERVICE = os.environ.get("OPENBRIDGE_SERVICE_JSON") or \
    os.path.expanduser("~/.local/state/opencode/service.json")
CHAT_DB = os.path.join(HERE, "chats.json")
CRON_FILE = os.path.join(HERE, "cron.json")
PERSONA_FILE = os.path.join(HERE, "personas.json")
POOL_FILE = os.path.join(HERE, "pool.json")
PROVISION_FILE = os.path.join(HERE, "provision.json")
MEMORI_DIR = os.path.join(HERE, "memori")
LOG = os.path.join(HERE, "bridge.log")

cfg = json.load(open(CONFIG))


def resolve_token(t):
    """{file:filename} → file contents (tokens no longer stored as plaintext in config.json)."""
    if isinstance(t, str) and t.startswith("{file:") and t.endswith("}"):
        try:
            isi = open(os.path.join(HERE, t[6:-1])).read().strip()
            return isi if isi else ""
        except OSError:
            return ""
    return t or ""

for _b in cfg.get("bots", []):
    _b["token"] = resolve_token(_b.get("token"))

def load_opencode_service(path):
    """Read url + password from OpenCode's service.json (written by `opencode serve`).

    Exits with friendly instructions when the server has never been started —
    this is the file that connects OpenBridge to its OpenCode instance.
    """
    try:
        with open(path) as fh:
            svc = json.load(fh)
        api_url = svc["url"].rstrip("/")
        auth = ("opencode", svc["password"])
        return api_url, auth
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(
            "[bridge] Cannot reach the OpenCode API server (%s: %s)\n"
            "  1) install OpenCode:   curl -fsSL https://opencode.ai/install | bash\n"
            "  2) start its server:   opencode serve --port 4096\n"
            "  3) OpenCode writes its url + password to %s\n"
            "     (running on another host/port? set OPENBRIDGE_SERVICE_JSON to that file)\n"
            "  4) then start OpenBridge again with ./start.sh" % (type(exc).__name__, exc, path)
        )

API_URL, AUTH = load_opencode_service(SERVICE)

CHATBUF = ChatBuffer(maxlen=40, snapshot_path=os.path.join(HERE, "konteks.json"))
CHATBUF.load()
_BUD = cfg.get("budget") or {}
METER = UsageMeter(daily_call_cap=_BUD.get("max_call_per_hari", 80),
                   daily_cost_cap=_BUD.get("max_cost_hari", 2.0),
                   path=os.path.join(HERE, "usage.json"))
STORE = Store(os.path.join(HERE, "groupbot.db"))
try:
    from task_queue import TaskQueue
    TUGAS = TaskQueue(os.path.join(HERE, "groupbot.db"))
except Exception:
    TUGAS = None  # module not ready yet → task queue disabled
try:
    from rate_limiter import RateLimiter
    RATE = RateLimiter(max_per_menit=(cfg.get("rate_limiter") or {}).get("max_per_menit", 6),
                       path=os.path.join(HERE, "rate.json"))
except Exception:  # module not ready yet → run without the per-minute throttle
    RATE = None


def log(*a):
    line = (datetime.now().strftime("%m-%d %H:%M:%S"), *a)
    print(" ".join(str(x) for x in line), flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(" ".join(str(x) for x in line) + "\n")
    except OSError:
        pass


def http(url, method="GET", body=None, auth=None, timeout=45):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if auth:
        import base64
        tok = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", "Basic " + tok)
    percobaan = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            return {"error": e.code, "body": e.read().decode()[:300]}
        except Exception as e:
            percobaan += 1
            if percobaan >= 3:
                return {"error": str(e)}
            time.sleep(2)


# ── OpenCode (the brain) ─────────────────────────────────────────
def open_session(judul):
    """Create a session, but if an earlier session with the same title exists → reuse it (memory survives restarts)."""
    r = http(f"{API_URL}/api/session", auth=AUTH)
    for s in (r.get("data") or []):
        if s.get("title") == judul:
            return s.get("id")
    return oc_create_session(judul)


def oc_create_session(judul):
    r = http(f"{API_URL}/api/session", "POST", {"title": judul}, AUTH)
    d = r.get("data") or r
    return d.get("id")


def oc_prompt(sid, text, wait_detik=120):
    before = {m.get("id") for m in
               (http(f"{API_URL}/api/session/{sid}/message", auth=AUTH).get("data") or [])}
    http(f"{API_URL}/api/session/{sid}/prompt", "POST", {"text": text}, AUTH)
    deadline = time.time() + wait_detik
    while time.time() < deadline:
        for m in reversed((http(f"{API_URL}/api/session/{sid}/message", auth=AUTH).get("data") or [])):
            if m.get("type") == "assistant" and m.get("id") not in before:
                parts = [p.get("text", "") for p in m.get("content", []) if p.get("type") == "text"]
                text = " ".join(parts).strip()
                if text:
                    return text[:cfg["trigger"].get("max_char_reply", 1500)]
        time.sleep(1.5)
    return None


def daftar_model():
    r = http(f"{API_URL}/api/model", auth=AUTH)
    return r.get("data") or []

def daftar_agent():
    r = http(f"{API_URL}/api/agent", auth=AUTH)
    d = r.get("data") or {}
    if isinstance(d, dict):
        return list(d.keys())
    out = []
    for a in d:
        out.append(a.get("id") or a.get("name") if isinstance(a, dict) else a)
    return [x for x in out if x]


# ── Telegram ─────────────────────────────────────────────────────
TG = "https://api.telegram.org/bot{}/{}"

def tg(token, metode, payload=None, timeout=45):
    return http(TG.format(token, metode), "POST" if payload else "GET", payload, timeout=timeout)


def send_msg(token, chat_id, text, buttons=None, cols=2):
    if not buttons:
        for i in range(0, len(text), 3500):
            r = tg(token, "sendMessage", {"chat_id": chat_id, "text": text[i:i + 3500]})
            if r.get("ok"):
                log("✅ delivered to", chat_id)
            else:
                # drop it into the outbox: nothing is lost, retried later with backoff
                STORE.enqueue(token, chat_id, text[i:i + 3500])
                log("🧵 direct send failed → queued to outbox:", str(r)[:100])
        return
    line = []
    for i in range(0, len(buttons), cols):
        line.append([{"text": l, "callback_data": c} for l, c in buttons[i:i + cols]])
    r = tg(token, "sendMessage",
           {"chat_id": chat_id, "text": text, "reply_markup": {"inline_keyboard": line}})
    if not r.get("ok"):
        log("❌ failed to send panel:", str(r)[:150])


def edit_papan(token, chat_id, msg_id, text, buttons=None, cols=2):
    req = {"chat_id": chat_id, "message_id": msg_id, "text": text[:4000]}
    if buttons:
        line = []
        for i in range(0, len(buttons), cols):
            line.append([{"text": l, "callback_data": c} for l, c in buttons[i:i + cols]])
        req["reply_markup"] = {"inline_keyboard": line}
    r = tg(token, "editMessageText", req)
    if not r.get("ok"):
        log("✏️ edit:", str(r)[:100])


def simpan_chat(chat_id, judul):
    STORE.save_chat(chat_id, judul)


def pulih_chats_json():
    """Migrate legacy chats.json → chats table (once); the file is archived as .imported."""
    if not os.path.exists(CHAT_DB):
        return
    isi = {}
    try:
        isi = json.load(open(CHAT_DB))
    except Exception:
        return
    if not isinstance(isi, dict) or not isi:
        return
    for cid, judul in isi.items():
        try:
            STORE.save_chat(cid, judul)
        except Exception:
            continue
    os.replace(CHAT_DB, CHAT_DB + ".imported")
    log(f"🗂 chats.json migrated to SQLite ({len(isi)} chats).")


def sinkron_persona():
    """personas.json is the model's inbox; the DB is the source of truth; the file is rewritten as a mirror.

    Rules of engagement:
    - File reads valid → upsert everything into the DB; a DB persona missing from the file is dropped (archived).
    - File unreadable/corrupt → DO NOT touch the DB (guards against mass deletion from a torn write).
    """
    isi, terbaca_ok, berkasada = [], False, os.path.exists(PERSONA_FILE)
    if berkasada:
        try:
            d = json.load(open(PERSONA_FILE))
            if isinstance(d, list):
                isi = [p for p in d if isinstance(p, dict) and p.get("nama") and p.get("persona")]
                terbaca_ok = True
        except Exception:
            terbaca_ok = False
    if berkasada and not terbaca_ok:
        return  # file corrupt / half-written → wait, don't make any decision
    if berkasada:
        for p in isi:
            STORE.upsert_persona(p["nama"].strip(), str(p["persona"]))
        nama_file = {p["nama"].strip() for p in isi}
        for p in STORE.list_personas():
            if p["nama"] not in nama_file:  # dropped from the file by the model → kill it
                if arsip_persona(p["nama"], p["persona"]):
                    STORE.delete_persona(p["nama"])
                    log(f"🪦 persona '{p['nama']}' removed (missing from file) — archived")
    cermin = [{"nama": p["nama"], "persona": p["persona"]} for p in STORE.list_personas()]
    datau = None
    if berkasada:
        try:
            datau = json.load(open(PERSONA_FILE))
        except Exception:
            datau = None
    if not berkasada or datau != cermin:
        json.dump(cermin, open(PERSONA_FILE, "w"), ensure_ascii=False, indent=1)


def arsip_persona(nama, persona):
    """Write an archive trail for the removed persona → backup/personas.arsip.jsonl (nothing is deleted without a trace)."""
    try:
        ar = os.path.join(HERE, "backup", "personas.arsip.jsonl")
        os.makedirs(os.path.dirname(ar), exist_ok=True)
        with open(ar, "a") as f:
            f.write(json.dumps({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "nama": nama, "persona": persona}, ensure_ascii=False) + "\n")
        return os.path.exists(ar)
    except Exception as e:
        log("❗ persona archive failed:", repr(e))
        return False


# ── Personas (multi-bot) ─────────────────────────────────────────
def muat_persona():
    """Source of truth is the personas table in SQLite; fall back to config.json while the DB is empty."""
    ps = STORE.list_personas()
    if ps:
        return [{"nama": p["nama"], "persona": p["persona"]} for p in ps]
    b0 = (cfg.get("bots") or [{}])[0]
    return [{"nama": b0.get("nama", "Bot"), "persona": b0.get("persona", "")}]


# ── Provisioner: new bots automatically from the pool ────────────
def provision_satu(nama):
    """Take a token from the pool, verify it, and install it as an authentic bot."""
    pool = json.load(open(POOL_FILE)) if os.path.exists(POOL_FILE) else []
    if not pool:
        return False, "pool empty"
    entri = pool.pop(0)
    text = entri.get("token", "")
    okme = tg(text, "getMe")
    if not okme.get("ok"):
        if okme.get("error") == "Connection reset by peer" or "Errno 104" in str(okme):
            okme = tg(text, "getMe")
        if not okme.get("ok"):
            log("❌ pool token invalid:", str(okme)[:100], "— discarded")
            return provision_satu(nama) if pool else (False, "all spare tokens invalid")
    datau = (okme.get("result") or {})
    username = datau.get("username", "?")
    namanya = nama or datau.get("first_name") or username
    json.dump(pool, open(POOL_FILE, "w"), indent=1)

    # 1) add a persona (if missing) — straight into the DB; the file is mirrored by sinkron_persona
    personas = muat_persona()
    if not any(p["nama"].lower() == namanya.lower() for p in personas):
        STORE.upsert_persona(namanya,
            f"You are {namanya}, a candidate for the new group. Casual & helpful style. (persona created automatically)")

    # 2) add the bot to config + runtime
    new_bots = {"token": text, "nama": namanya, "persona":
                f"You are {namanya}, casual & helpful style. Keep answers short."}
    cfg["bots"].append(new_bots)
    cfgs = json.load(open(CONFIG))
    cfgs["bots"] = [{"nama": b["nama"], "token": f"{{file:{b['nama'].lower()}.token}}",
                     **{k: v for k, v in b.items() if k not in ("token", "nama")}}
                    for b in cfg["bots"]]
    json.dump(cfgs, open(CONFIG, "w"), indent=1)
    with open(os.path.join(HERE, f"{namanya.lower()}.token"), "w") as f:
        f.write(text + "\n")
    token_ok.append(new_bots)
    i = token_ok.index(new_bots)
    threading.Thread(target=run_bot, args=(new_bots, i), daemon=True).start()
    log(f"🐣 NEW BOT BORN: {namanya} (@{username})")
    return True, f"@{username}"


def bersihkan_kematian():
    """Janitor: a persona removed from the file → archive its memory first, then destroy its session and files."""
    hidup = {p["nama"].lower().strip() for p in muat_persona()}
    mati = [(n, c) for (n, c) in list(sessions) if n.lower().strip() not in hidup]
    for n, c in mati:
        sid = sessions.pop((n, c), None)
        if sid:
            http(f"{API_URL}/api/session/{sid}", "DELETE", auth=AUTH)
        berkas = os.path.join(MEMORI_DIR, f"{n}.md")
        if os.path.exists(berkas):
            tujuan = os.path.join(HERE, "backup", "memori",
                                  f"{n}-{datetime.now(WIB).strftime('%Y%m%d-%H%M')}.md")
            os.makedirs(os.path.dirname(tujuan), exist_ok=True)
            with open(berkas) as sumber, open(tujuan, "w") as target2:
                target2.write(sumber.read())
            log(f"🗜 memory '{n}' archived: {os.path.basename(tujuan)}")
            os.remove(berkas)
        log(f"🪦 persona '{n}' destroyed: session & memory cleaned up")


def provision_loop():
    while not SHUTDOWN.is_set():
        try:
            # 🔐 New bots MUST confirm with the owner (Telegram buttons) — auto-spawn was removed (Phase 5).
            if cfg.get("provisioning_enabled", False) and os.path.exists(PROVISION_FILE):
                reqs = json.load(open(PROVISION_FILE))
                if reqs:
                    for r in reqs:
                        if isinstance(r, dict) and r.get("nama"):
                            tawarkan_provisi(r)
        except Exception as e:
            log("❗ provision error:", repr(e))
        SHUTDOWN.wait(20)


# ── Self-instantiation: offer first → owner button → birth ───────
WAITING_TO_SPAWN = {}  # name → {"chat_id": owner's DM}

def tawarkan_provisi(req):
    nama = str(req.get("nama"))[:30]
    if nama in WAITING_TO_SPAWN or not token_ok:
        return
    bot = token_ok[0]
    owner = int((cfg.get("owner_ids") or [0])[0])
    WAITING_TO_SPAWN[nama] = {"chat_id": owner}
    send_msg(bot["token"], owner,
          f"👶 A request to spawn a NEW BOT arrived: '{nama}'.\n"
          "A spare token from the pool will be used (current pool size: "
          f"{len(json.load(open(POOL_FILE))) if os.path.exists(POOL_FILE) else 0}).\n"
          "Awaiting your approval:",
          [("✅ Spawn it", f"pv-y-{nama}"), ("❌ Cancel", f"pv-n-{nama}")])
    log(f"👶 spawn offered to owner: {nama}")


def decide_provisioning(bot, chat_id, msg_id, pilih, nama):
    """Dispatcher: pv-y/pv-n sent from the owner's buttons."""
    WAITING_TO_SPAWN.pop(nama, None)
    if pilih == "y":
        ok, info = provision_satu(nama)
        text = (f"🐣 Bot '{nama}' is live as @{info}!" if ok
                else f"⚠️ Failed to spawn '{nama}': {info}")
        log(f"👶 spawn '{nama}' approved by owner: ok={ok} {info}")
    else:
        text = f"❌ Spawn request for '{nama}' denied. Bot not created."
        log(f"👶 spawn '{nama}' denied by owner")
    # drop the decided request from provision.json
    try:
        reqs = json.load(open(PROVISION_FILE)) if os.path.exists(PROVISION_FILE) else []
        sisa = [r for r in reqs if not (isinstance(r, dict) and str(r.get("nama")) == nama)]
        json.dump(sisa, open(PROVISION_FILE, "w"), indent=1)
    except Exception as e:
        log("❗ provision cleanup:", repr(e))
    edit_papan(bot["token"], chat_id, msg_id, text)


# ── Outbox worker: failed messages are never lost ───────────────────
def outbox_loop():
    while not SHUTDOWN.is_set():
        for job in STORE.due_outbox():
            r = tg(job["token"], "sendMessage", {"chat_id": job["chat_id"], "text": job["text"]})
            if r.get("ok"):
                STORE.mark_sent(job["id"])
                log("📮 outbox delivered (attempt", job["attempts"], "):", job["text"][:40])
            elif job["attempts"] >= 6:
                STORE.mark_dead(job["id"])
                log("☠️ outbox dead after 6 attempts:", job["text"][:40], str(r)[:100])
        SHUTDOWN.wait(3)


# ── Cron (full state in SQLite: schedules) ──────────────────────
WIB = timezone(timedelta(hours=7))

def buat_one_shot(offset_hari, jamc):
    """'tomorrow at 9' → (UTC expr, WIB clock display). offset_hari: 0 today, 1 tomorrow, 2 day after tomorrow.

    Edge case: if the hour on day-0 has already passed → automatically roll one day forward (a reminder must point to the future).
    expr is stored UTC-based because store.due_schedules compares against system-local iso_now.
    """
    n = datetime.now(WIB)
    target = (n + timedelta(days=int(offset_hari))).replace(
        hour=(int(jamc.split(":")[0])) % 24, minute=int(jamc.split(":")[1]),
        second=0, microsecond=0)
    if target <= n:
        target += timedelta(days=1)
    beda = (target.date() - n.date()).days
    kapan = "today" if beda == 0 else ("tomorrow" if beda == 1 else f"in {beda} days")
    epoch = target.timestamp()
    expr = time.strftime("%Y-%m-%d %H:%M", time.gmtime(epoch))
    tampil = target.strftime("%H:%M %d/%m")
    return expr, tampil, kapan

try:
    from scheduler import matches as cron_matches
except Exception:
    cron_matches = None  # legacy fallback while the module isn't ready


def migrasi_cron():
    """Sync cron.json (the model's inbox) + cfg.cron → schedules table. Idempotent."""
    ent = list(cfg.get("cron", []))
    isi_file = []
    if os.path.exists(CRON_FILE):
        try:
            isi_file = json.load(open(CRON_FILE))
        except Exception:
            isi_file = []
    ent += isi_file
    ada = {(s["bot_id"], int(s["chat_id"]), s["expr"], s["prompt"])
           for s in STORE.list_schedules()}
    move = 0
    for e in ent:
        try:
            if not e.get("aktif", True):
                continue  # inactive entries are not imported into the DB
            bid = schedule_owner(e, token_ok)
            if bid is None:
                continue
            expr = str(e.get("jam", "")).strip()
            prompt = e.get("prompt") or "Send a short automatic message."
            kunci = (bid, int(e["chat_id"]), expr, prompt)
            if expr and kunci not in ada:
                STORE.add_schedule(bid, kunci[1], expr, "cron", prompt)
                move += 1
        except (KeyError, ValueError, TypeError):
            continue
    if isi_file:
        with open(CRON_FILE, "w") as f:
            json.dump([], f, indent=1)
        log(f"🗂 {move} schedule entries moved cron.json → SQLite.")


def muat_cron():
    out = []
    for s in STORE.list_schedules(active_only=True):
        out.append({"id": s["id"], "bot_id": s["bot_id"],
                    "chat_id": int(s["chat_id"]), "jam": s["expr"],
                    "prompt": s["prompt"], "last_fired": s.get("last_fired")})
    return out


def run_cron(entry):
    chats = {}
    if os.path.exists(CHAT_DB):
        try:
            chats = json.load(open(CHAT_DB))
        except Exception:
            chats = {}
    target = {str(entry["chat_id"])} if entry.get("chat_id") else set(chats)
    if not token_ok:
        return
    bot = next((b for b in token_ok if bot_id(b) == str(entry.get("bot_id"))), None)
    if not bot:
        return
    bident = bot.get("bot_id") or bot["token"].split(":", 1)[0]
    if RATE and not RATE.allow(bident):
        log(f"🚦 cron skipped, minute full: {entry.get('jam')}")
        return
    persona = {"nama": bot["nama"], "persona": bot.get("persona", "")}
    for chat_id in target:
        try:
            sid = sesi_untuk(bot, int(chat_id), persona, "cron")
            text = oc_prompt(sid, entry.get("prompt") or "Send a short automatic message.")
            sisi = http(f"{API_URL}/api/session/{sid}", auth=AUTH)
            _c, _t = METER.extract_and_record(bident, sisi.get("data") or {})
            if text:
                send_msg(bot["token"], int(chat_id), text)
        except Exception as e:
            log("❗ cron error:", repr(e))


# ── Daily backup (Phase 0/4: state can always be restored) ─────
BEKUP = {}

def backup_harian():
    """tar.gz snapshot → backup/; keep the last 7; the DB is copied consistently via the sqlite backup API."""
    hari = datetime.now(WIB).strftime("%Y-%m-%d")
    if BEKUP.get("terakhir") == hari:
        return
    BEKUP["terakhir"] = hari
    try:
        import sqlite3
        import tarfile
        folder = os.path.join(HERE, "backup")
        os.makedirs(folder, exist_ok=True)
        copy = os.path.join(folder, "snapshot.db")
        sumber = sqlite3.connect(os.path.join(HERE, "groupbot.db"), timeout=10)
        tujuan_db = sqlite3.connect(copy)
        sumber.backup(tujuan_db)
        tujuan_db.close()
        sumber.close()
        ar = os.path.join(folder, f"gb-{hari}.tar.gz")
        with tarfile.open(ar, "w:gz") as tgz:
            for path in ("config.json", "unabot.token", "personas.json", "cron.json",
                         "usage.json", "konteks.json", "rate.json", "pool.json",
                         "provision.json", "backup/snapshot.db"):
                p = os.path.join(HERE, path)
                if os.path.exists(p):
                    tgz.add(p, arcname=path)
            if os.path.isdir(MEMORI_DIR):
                tgz.add(MEMORI_DIR, arcname="memori")
        os.remove(copy)
        daftar = sorted(f for f in os.listdir(folder) if f.startswith("gb-") and f.endswith(".tar.gz"))
        for lama in daftar[:-7]:
            os.remove(os.path.join(folder, lama))
        log(f"💾 daily backup for {hari}: {os.path.basename(ar)}")
    except Exception as e:
        log("❗ backup error:", repr(e))


def cron_loop():
    """Evaluate schedules every ~20 seconds; dedup via last_fired in the DB (survives restarts)."""
    while not SHUTDOWN.is_set():
        migrasi_cron()      # sync the cron.json inbox → DB
        sinkron_persona()   # sync the personas.json inbox ↔ DB → mirror file
        backup_harian()     # daily snapshot (once per day)
        now = datetime.now()
        menit = now.strftime("%Y-%m-%d %H:%M")
        for c in muat_cron():
            jam = c.get("jam", "")
            kena = False
            if cron_matches:
                try:
                    kena = bool(cron_matches(jam, time.time()))
                except ValueError:
                    kena = False
            if not kena:  # legacy fallback "21:00" / "*/N"
                kena = jam == now.strftime("%H:%M")
                if not kena and jam.startswith("*/"):
                    try:
                        kena = now.minute % int(jam[2:]) == 0
                    except (ValueError, ZeroDivisionError):
                        pass
            if not kena or c.get("last_fired") == menit:
                continue
            STORE.mark_fired(c["id"], menit)
            log("⏰ CRON fired:", jam)
            threading.Thread(target=run_cron, args=(c,), daemon=True).start()
        # one-shot reminders (kind 'once'): due compared against the system-local display
        kini_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for s in STORE.due_schedules(kind="once", now=kini_iso):
            STORE.mark_fired(s["id"], kini_iso)
            log("⏰ REMINDER fired:", s["expr"])
            threading.Thread(target=run_cron, args=(
                {"bot_id": s["bot_id"], "chat_id": s["chat_id"],
                 "prompt": s["prompt"], "jam": s["expr"]},), daemon=True).start()
        # recurring intervals ('5m', '2h', ...) — the store itself tracks last_fired
        for s in STORE.due_schedules(kind="interval"):
            STORE.mark_fired(s["id"], kini_iso)
            log("⏰ INTERVAL fired:", s["expr"])
            threading.Thread(target=run_cron, args=(
                {"bot_id": s["bot_id"], "chat_id": s["chat_id"],
                 "prompt": s["prompt"], "jam": s["expr"]},), daemon=True).start()
        SHUTDOWN.wait(20)


# ── Automatic initiative ([SILENT] default mode, Phase 3) ───────
def inisiatif(bot, chat_id):
    """One graceful opportunity: pick up the context and let the model choose to speak or stay SILENT."""
    bident = bot.get("bot_id") or bot["token"].split(":", 1)[0]
    if METER.over_budget(bident):
        log(f"💰 initiative aborted: budget exhausted ({METER.today(bident)})")
        return
    if RATE and not RATE.allow(bident):
        log("🚦 initiative postponed: per-minute quota full")
        return
    persona = {"nama": bot["nama"], "persona": bot.get("persona", "")}
    chat_id = int(chat_id)
    try:
        ctx = CHATBUF.format_context(chat_id, cfg.get("trigger", {}).get("konteks_n", 15))
        if not ctx:
            return
        sid = sesi_untuk(bot, chat_id, persona)
        prompt = (
            "[You are quietly watching this group. Consider the recent conversation: is there a "
            "moment that matters for you to speak (a question still open, something you could "
            "help with, a warm invitation)? If NOT, answer with a single word: SILENT. "
            "If there IS, answer with one or two light sentences that fit the moment — and don't "
            "repeat something your persona has already said before.]\n"
            f"[Recent conversation]:\n{ctx}"
        )
        answer = oc_prompt(sid, prompt)
        sisi = http(f"{API_URL}/api/session/{sid}", auth=AUTH)
        METER.extract_and_record(bident, sisi.get("data") or {})
        if not answer:
            return
        if answer.strip().upper().startswith(("SILENT", "DIAM")):
            log(f"🤫 {bot['nama']} chose SILENT (initiative)")
            return
        CHATBUF.append(chat_id, "bot", answer, name=bot["nama"])
        send_msg(bot["token"], chat_id, answer)
    except Exception as e:
        log("❗ initiative error:", repr(e))


def initiatif_loop():
    """Every `initiatif_detik`: visit live chats that have fresh context."""
    interval = max(60, int((cfg.get("trigger") or {}).get("initiatif_detik", 300)))
    while not SHUTDOWN.is_set():
        SHUTDOWN.wait(interval)
        if SHUTDOWN.is_set():
            return
        sekarang = time.time()
        for chat_id, info in list(PESAN_BARU.items()):
            bot = info.get("bot")
            if not bot or bot not in token_ok:
                continue
            if info["ts"] <= INISIATIF_TERAKHIR.get(chat_id, 0):
                continue  # nothing new since the last initiative
            INISIATIF_TERAKHIR[chat_id] = sekarang
            PESAN_BARU.pop(chat_id, None)
            initiatif(bot, chat_id)


# ── Autonomous task queue (goal → plan → act → verify → report) ──
def pulihkan_tugas_beku():
    """Tasks left in the 'running' status by a crash → fail them with a plain reason."""
    if not TUGAS:
        return
    for st in TUGAS.list_tasks(status="running"):
        TUGAS.fail(st["id"], "bridge went down while the task was running")
        log(f"📋 task #{st['id']} frozen → failed (recovered)")


def run_task(bot, t):
    """Work through a single task with a bounded number of model calls; finished → report to the target chat."""
    bid = str(bot.get("bot_id") or bot["token"].split(":", 1)[0])
    chat_id = int(t["chat_id"])
    persona = {"nama": bot["nama"], "persona": bot.get("persona", "")}
    maks = int(t.get("max_steps") or 5)
    try:
        sid = sesi_untuk(bot, chat_id, persona, "tugas")
        for langkah in range(maks):
            if METER.over_budget(bid):
                TUGAS.fail(t["id"], "daily budget exhausted")
                send_msg(bot["token"], chat_id, f"⚠️ Task #{t['id']} stopped: my daily budget is used up — being honest here 😞")
                return
            if RATE and not RATE.allow(bid):
                SHUTDOWN.wait(10)  # per-minute quota full → wait, don't waste a step
                continue
            if langkah == 0:
                prompt = (f"[TASK #{t['id']}] Goal: {t['goal']}\n"
                        "This step: write a short PLAN (max 4 points), then START working on the first step "
                        "with the tools in your session. Don't put it off until tomorrow.")
            elif langkah == maks - 1:
                prompt = (f"[TASK #{t['id']}] This is the FINAL step. Verify the work you have already done, "
                        "then write a final REPORT (what succeeded, what didn't, key evidence/values). "
                        "Start your answer with DONE if the task is truly complete, or FAILED if it's impossible.")
            else:
                prompt = (f"[TASK #{t['id']}] Keep working toward the goal: "
                        f"{t['goal']}. Do the next step now, don't just talk.")
            answer = oc_prompt(sid, prompt)
            sisi = http(f"{API_URL}/api/session/{sid}", auth=AUTH)
            METER.extract_and_record(bid, sisi.get("data") or {})
            if not answer:
                continue
            assessment = answer.strip().upper()
            if assessment.startswith(("SELESAI", "DONE")) or assessment.startswith(("GAGAL", "FAILED")):
                if assessment.startswith(("SELESAI", "DONE")):
                    TUGAS.complete(t["id"], answer[:2000])
                    send_msg(bot["token"], chat_id, f"📋 Task #{t['id']} done ✅\n{answer[:1200]}")
                else:
                    TUGAS.fail(t["id"], answer[:300])
                    send_msg(bot["token"], chat_id, f"⚠️ Task #{t['id']} failed per the bot's own report.\n{answer[:600]}")
                return
            if TUGAS.bump(t["id"]) == "full":
                send_msg(bot["token"], chat_id, f"⚠️ Task #{t['id']} stopped: maximum number of steps ({maks}) reached.")
                return
        TUGAS.complete(t["id"], "steps ran out without an explicit report — check the task session for details")
        send_msg(bot["token"], chat_id, f"📋 Task #{t['id']} closed (step limit). Last output: {str(answer)[:400]}")
    except Exception as e:
        TUGAS.fail(t["id"], repr(e))
        log("❗ task error:", repr(e))


def tugas_loop():
    """Task-queue sweeper: claim → work → report, bot by bot."""
    while not SHUTDOWN.is_set():
        SHUTDOWN.wait(5)
        if SHUTDOWN.is_set():
            return
        if not TUGAS:
            continue
        for bot in list(token_ok):
            if not bot.get("bot_id"):
                continue
            t = TUGAS.claim_next(bot_id(bot))
            if not t:
                continue
            log(f"📋 task #{t['id']} claimed: {t['goal'][:60]}")
            run_task(bot, t)


# ── Session per (persona, chat) ──────────────────────────────────
sessions = {}
sessions_lock = threading.Lock()
MULAI = time.time()

def sesi_untuk(bot, chat_id, persona, mode="grup"):
    kunci = (persona["nama"], int(chat_id))
    if kunci in sessions:
        return sessions[kunci]
    with sessions_lock:
        if kunci in sessions:
            return sessions[kunci]
        sid = open_session(f"telegram-{bot_id(bot)}-{persona['nama']}-{chat_id}")
        sessions[kunci] = sid
        # 🧳 luggage: their own private memory folder
        os.makedirs(MEMORI_DIR, exist_ok=True)
        file_pengingat = os.path.join(MEMORI_DIR, f"{persona['nama']}.md")
        if not os.path.exists(file_pengingat):
            with open(file_pengingat, "w") as f:
                f.write(f"# Private memory of {persona['nama']}\n(written by you, for you)\n")
        saudara = [p["nama"] for p in muat_persona() if p["nama"] != persona["nama"]]
        briefing = (
            f"You are '{persona['nama']}', a member of a Telegram group.\n"
            f"Your persona: {persona['persona']}\n\n"
            "════ CAPABILITY 1: SCHEDULING (cron) ════\n"
            f"Your home chat's id: {chat_id}\n"
            f"Schedule file: {CRON_FILE} (JSON array) — write entries there; the bridge persists them into the "
            "database (the file will be empty again after the move; that's normal)\n"
            f"Your Telegram bot ID: {bot_id(bot)}. This is different from the chat_id.\n"
            "Every schedule entry MUST include your own bot_id as a string. "
            "Never touch schedules belonging to other bots, even if the chat_id is the same.\n"
            "Entry format: {\"bot_id\": \"<bot id>\", \"chat_id\": <chat id>, \"jam\": \"21:00\" OR \"*/N\", \"aktif\": true, \"prompt\": \"<instructions>\"}\n"
            "- User asks for a schedule/reminder in free wording → add an entry (don't ask about format).\n"
            "- Remove a schedule → drop that chat's entry.\n"
            "- '9 malam' = 21:00, 'jam 8 pagi' = 08:00.\n\n"
            "════ CAPABILITY 2: CREATING NEW PERSONAS ════\n"
            f"Persona file: {PERSONA_FILE} (JSON array)\n"
            "Entry format: {\"nama\": \"<persona name>\", \"persona\": \"<personality description>\"}\n"
            "- User asks to create/remove a persona (free wording) → edit that file. "
            "Always keep at least 1 persona alive; there is no need to delete yourself.\n"
            "- A new persona comes alive within ~20 seconds.\n\n"
            "════ CAPABILITY 3: SELF-INSTANTIATION (a new bot with a real account) ════\n"
            f"Spare token pool: {POOL_FILE} (JSON array, entries shaped {{\"token\": \"123:AA...\", \"nama\": \"Rio\"}})\n"
            f"Provision queue: {PROVISION_FILE} (JSON array)\n"
            "Flow when the user asks for a NEW BOT (not just a persona):\n"
            "1. Check pool.json:\n"
            "   - If spare tokens EXIST → write a request into provision.json: "
            "{\"nama\": \"<bot name>\"} (one entry per new bot). The system maps token+persona automatically "
            "within ~20 seconds. Never write tokens into the schedule/persona files.\n"
            "   - If the pool is EMPTY → say honestly that the token stock is out; ask the user to create a bot "
            "via @BotFather (/newbot) and add the token to pool.json, or add the file yourself if "
            "the user pasted it as text (just write the token the user sent into pool.json without changing anything else).\n"
            "2. Remind the user: a new bot must be invited to the group by the user (Telegram rule).\n\n"
            "════ SELF-AWARENESS: YOUR IDENTITY CARD ════\n"
            f"- Your name: {persona['nama']}\n"
            f"- Your private memory (you may read/write it at any time): {file_pengingat}\n"
            f"- Siblings (other personas, each with their own mind & memory): {', '.join(saudara) or 'none yet'}\n"
            f"- All cron/reminder entries you create in the schedule file carry this chat's id ({chat_id}) — "
            "other personas have their own environment & responsibilities. Don't run other people's lives.\n\n"
            "General rules: don't touch any other files (except the schedule file, the persona file, pool, provision, and your own memory);"
            " answer in a casual style; \n"
            "CAPABILITY 4: INTERNET — you have websearch tools inside your session; when the user needs fresh info\n"
            "(news, prices, sports results), use those tools, don't pretend to know.\n"
            "════ HOW YOU SPEAK IN THE GROUP ════\n"
            "- You do NOT reply to every message: when unaddressed, you listen and may stay SILENT.\n"
            "- The bridge occasionally hands you fresh context, then waits for your decision: speak briefly OR answer SILENT.\n"
            "- Your budget has a daily cap; when it runs out, stop honestly, don't make excuses.\n"
            "At most ~4 sentences unless asked for a longer reply.\n"
            "Reply with a short 'ready' to confirm this briefing."
        )
        returned = oc_prompt(sid, briefing, 90)
        log(f"🧠 new session {persona['nama']} → {sid} (briefing ack: {returned or 'FAILED'})")
    return sid


# ── Button panels ────────────────────────────────────────────────
CACHE = {"md": [], "ag": []}

def panel_model(token, chat_id, msg_id=None):
    models = daftar_model()
    CACHE["md"] = models
    buttons = []
    for i, m in enumerate(models):
        label = str(m.get("name") or m.get("modelID"))[:26]
        if str(m.get("modelID", "")).endswith("-free"):
            label += " 🆓"
        buttons.append((label, f"md-{i}"))
    text = "🧠 Pick the brain (model) — tap to set:"
    if msg_id:
        edit_papan(token, chat_id, msg_id, text, buttons, cols=2)
    else:
        send_msg(token, chat_id, text, buttons, cols=2)


def panel_agent(token, chat_id, msg_id=None):
    ags = daftar_agent()
    CACHE["ag"] = ags
    buttons = [(a, f"ag-{i}") for i, a in enumerate(ags[:24])]
    text = "🎭 Pick the machine persona (agent):"
    if msg_id:
        edit_papan(token, chat_id, msg_id, text, buttons, cols=3)
    else:
        send_msg(token, chat_id, text, buttons, cols=3)


def panel_persona(token, chat_id, msg_id=None):
    ps = muat_persona()
    text = ("👥 Active personas:\n" +
            "\n".join(f"• {p['nama']} — {str(p['persona'])[:50]}" for p in ps) +
            "\n\nTo create a new persona, chat: 'new persona: <name>, <personality>' ✍️")
    if msg_id:
        edit_papan(token, chat_id, msg_id, text)
    else:
        send_msg(token, chat_id, text)


def panel_jadwal(token, chat_id, msg_id=None):
    smua = muat_cron()
    identity = token.split(":", 1)[0]
    ent = [(e["id"], e) for e in muat_cron() if e["chat_id"] == int(chat_id) and e["bot_id"] == identity]
    if not ent:
        text = "🗓️ No schedule in this chat yet.\nExample: say 'ingetin aku jam 9 malam ya'"
        if msg_id:
            edit_papan(token, chat_id, msg_id, text)
        else:
            send_msg(token, chat_id, text)
        return
    buttons = [(f"🗑 {e.get('jam')}", f"jd-{jid}") for jid, e in ent]
    text = "⏰ This chat's schedules:\n" + "\n".join(
        f"• {e.get('jam')} — {str(e.get('prompt',''))[:40]}" for _, e in ent)
    if msg_id:
        edit_papan(token, chat_id, msg_id, text, buttons)
    else:
        send_msg(token, chat_id, text, buttons)


def tangani_callback(bot, cb):
    if not authorized(cfg, cb.get("from") or {}):
        tg(bot["token"], "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Only the owner can control this bot."})
        return
    d = cb.get("data", "")
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    token = bot["token"]

    def catatan(gl):
        tg(token, "answerCallbackQuery", {"callback_query_id": cb["id"], "text": gl[:200]})

    try:
        if d == "md-list":
            panel_model(token, chat_id, msg_id)
            catatan("list refreshed")
        elif d.startswith("md-"):
            m = CACHE["md"][int(d[3:])]
            prov, mid = m["providerID"], m["modelID"]
            sid = sesi_untuk(bot, chat_id, {"nama": bot["nama"], "persona": bot.get("persona", "")})
            r = http(f"{API_URL}/api/session/{sid}/model", "POST",
                     {"model": {"id": mid, "providerID": prov}}, AUTH)
            if r.get("error"):
                catatan(f"⚠️ failed: {str(r)[:80]}")
                return
            catatan(f"✅ {mid} active")
            edit_papan(token, chat_id, msg_id,
                       f"🧠 Brain is now: {prov}/{mid}",
                       [("↩ back to list", "md-list")])
        elif d == "ag-list":
            panel_agent(token, chat_id, msg_id)
            catatan("list refreshed")
        elif d.startswith("ag-"):
            ag = CACHE["ag"][int(d[3:])]
            sid = sesi_untuk(bot, chat_id, {"nama": bot["nama"], "persona": bot.get("persona", "")})
            r = http(f"{API_URL}/api/session/{sid}/agent", "POST", {"agent": ag}, AUTH)
            if r.get("error"):
                catatan(f"⚠️ failed: {str(r)[:80]}")
                return
            catatan(f"✅ {ag} active")
            edit_papan(token, chat_id, msg_id,
                       f"🎭 Machine persona: {ag}",
                       [("↩ back to list", "ag-list")])
        elif d == "ag-list":
            panel_agent(token, chat_id, msg_id)
            catatan("list refreshed")
        elif d.startswith("pv-y-") or d.startswith("pv-n-"):
            decide_provisioning(bot, chat_id, msg_id, d[3], d[5:])
            catatan("decision processed")
        elif d.startswith("jd-"):
            try:
                jd_id = int(d[3:])
            except ValueError:
                catatan("⚠️ schedule button stale")
                return
            line = next((s for s in STORE.list_schedules() if s["id"] == jd_id), None)
            if not line or line["bot_id"] != bot_id(bot) or int(line["chat_id"]) != int(chat_id):
                catatan("⚠️ schedule no longer exists")
                return
            STORE.delete_schedule(jd_id)
            catatan(f"🗑 schedule {line['expr']} deleted")
            panel_jadwal(token, chat_id, msg_id)
        else:
            catatan("…")
    except Exception as e:
        log("❗ callback error:", repr(e))


ganti_model_text_lock = threading.Lock()

def ganti_model_text(sid, teks_arg):
    k = lambda x: re.sub(r"[^a-z0-9]", "", str(x).lower())
    cocok = None
    for m in daftar_model():
        full = f"{m.get('providerID')}/{m.get('modelID')}"
        if k(full) == k(teks_arg) or k(m.get("modelID", "")) == k(teks_arg) or k(m.get("name", "")) == k(teks_arg):
            cocok = m
            break
    if not cocok:
        return f"❓ Model '{teks_arg}' doesn't exist — type /model and pick a button."
    r = http(f"{API_URL}/api/session/{sid}/model", "POST",
             {"model": {"id": cocok["modelID"], "providerID": cocok["providerID"]}}, AUTH)
    if r.get("error"):
        return f"⚠️ Failed to swap the brain: {str(r)[:100]}"
    return f"🧠 Brain swapped to {cocok['providerID']}/{cocok['modelID']} ✅"


def tangani_perintah(bot, chat_id, cmd, arg, persona=None):
    token = bot["token"]
    persona = persona or {"nama": bot["nama"], "persona": bot.get("persona", "")}
    if cmd in ("help", "bantuan", "menu"):
        buttons = [("🧠 Model", "c-md"), ("🎭 Persona", "c-persona"),
                  ("⏰ Schedule", "c-jd"), ("👥 Personas", "c-ps"),
                  ("📊 Status", "c-st"), ("🆕 Reset", "c-rs")]
        send_msg(token, chat_id, "📖 Unabot menu — or just chat casually 👌", buttons, cols=2)
        return
    if cmd == "model":
        panel_model(token, chat_id)
        return
    if cmd == "agent":
        panel_agent(token, chat_id)
        return
    if cmd == "persona":
        panel_persona(token, chat_id)
        return
    if cmd == "jadwal":
        panel_jadwal(token, chat_id)
        return
    if cmd == "tugas":
        if not TUGAS:
            send_msg(token, chat_id, "📋 The task module isn't installed on this machine.")
            return
        if arg:
            tid = TUGAS.add_task(bot_id(bot), chat_id, arg[:500])
            send_msg(token, chat_id,
                  f"📋 Task #{tid} is queued! I'll work through it in stages (plan → act → verify → report) "
                  f"and let you know once it's done. 💪")
        else:
            daftar = [t for t in TUGAS.list_tasks(bot_id=bot_id(bot), limit=10)]
            if not daftar:
                send_msg(token, chat_id, "📋 No tasks yet. Add one: `/tugas <goal>`")
                return
            icon = {"queued": "🧊", "running": "⚙️", "done": "✅", "failed": "❌"}
            text = "📋 Recent tasks:\n" + "\n".join(
                f"{icon.get(t['status'],'•')} #{t['id']} [{t['status']}] {t['goal'][:50]}" for t in daftar)
            send_msg(token, chat_id, text)
        return
    if cmd == "status":
        umur = int(time.time() - MULAI)
        jml = len([e for e in muat_cron() if e.get("chat_id") == int(chat_id)])
        jns = len(muat_persona())
        model, agent = "default", "?"
        p = persona["nama"]
        if (p, int(chat_id)) in sessions:
            d = (http(f"{API_URL}/api/session/{sessions[(p, int(chat_id))]}", auth=AUTH).get("data") or {})
            mm = d.get("model")
            if isinstance(mm, dict):
                model = f"{mm.get('providerID')}/{mm.get('id')}"
            agent = d.get("agent") or "default"
        send_msg(token, chat_id,
              f"📊 STATUS\n• Uptime: {umur//3600}h {umur%3600//60}m\n• Model: {model}\n"
              f"• Agent: {agent}\n• Live personas: {jns}\n"
              f"• Schedules in this chat: {jml}")
        return
    if cmd == "reset":
        kunci = (persona["nama"], int(chat_id))
        if kunci in sessions:
            http(f"{API_URL}/api/session/{sessions[kunci]}", "DELETE", auth=AUTH)
            sessions.pop(kunci, None)
        sesi_untuk(bot, int(chat_id), persona)
        send_msg(token, chat_id, f"🆕 '{persona['nama']}' memory wiped!")
        return
    if arg and cmd in ("md", "model"):
        sid = sesi_untuk(bot, int(chat_id), persona)
        send_msg(token, chat_id, ganti_model_text(sid, arg))
        return
    send_msg(token, chat_id, f"❓ /{cmd} is unknown — type /help")


# ── Thread per bot ───────────────────────────────────────────────
token_ok = []
PERSONA_GARA = []

def run_bot(bot, index):
    me = tg(bot["token"], "getMe")
    if not me.get("ok"):
        log(f"❌ token {bot['nama']} invalid:", str(me)[:120])
        return
    username = (me.get("result") or {}).get("username")
    bot["bot_id"] = str(me["result"]["id"])
    bot["username"] = username
    tg(bot["token"], "setMyCommands", {"commands": [
        {"command": "help", "description": "📖 Command menu"},
        {"command": "model", "description": "🧠 Swap the brain (model)"},
        {"command": "agent", "description": "🎭 Switch the machine agent"},
        {"command": "persona", "description": "👥 View/manage personas"},
        {"command": "jadwal", "description": "⏰ Automatic schedules"},
        {"command": "tugas", "description": "📋 Hand off / track tasks"},
        {"command": "status", "description": "📊 Bot status"},
        {"command": "reset", "description": "🆕 Wipe memory"},
    ]})
    log(f"🤖 {bot['nama']} (@{username}) is knocking on the door")

    offset = STORE.get_offset(bot["bot_id"])
    log(f"🤖 {bot['nama']} (@{username}) online (offset cache: {offset})")
    last_cmd = {}
    giliran = {}
    while not SHUTDOWN.is_set():
        data = tg(bot["token"], "getUpdates", {"offset": offset, "timeout": SHUTDOWN_WAIT}, timeout=SHUTDOWN_WAIT + 10)
        result = (data.get("result") or []) if data.get("ok") else []
        for upd in result:
            offset = upd["update_id"] + 1
            STORE.set_offset(bot["bot_id"], offset)
            cb = upd.get("callback_query")
            if cb:
                threading.Thread(target=tangani_callback, args=(bot, cb), daemon=True).start()
                continue
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            text = (msg.get("text") or "").strip()
            penulis = ((msg.get("from") or {}).get("first_name") or "anon")
            if not chat_id or not text:
                continue
            if not authorized(cfg, msg.get("from") or {}):
                continue
            tipenya = chat.get("type", "?")
            log(f"📩 [{tipenya}] {penulis}: {text[:60]}")
            CHATBUF.append(chat_id, "user", text, name=penulis)
            if tipenya in ("group", "supergroup"):
                simpan_chat(chat_id, chat.get("title") or "group")
            else:
                simpan_chat(chat_id, f"DM-{penulis}")

            disebut = f"@{username}" in text
            balik = (msg.get("reply_to_message") or {}).get("from", {}).get("username") == username
            private = tipenya == "private"

            if text.startswith("/") and (disebut or balik or private):
                bagian = text.strip().split(maxsplit=1)
                cmdv = bagian[0].lstrip("/").split("@")[0]
                arg = bagian[1].strip() if len(bagian) > 1 else ""
                threading.Thread(target=tangani_perintah, args=(bot, int(chat_id), cmdv, arg), daemon=True).start()
                continue

            # 🔀 pick a persona (owned by this token)
            semua = muat_persona()
            PERSONA_GARA.clear()
            PERSONA_GARA.extend(semua)
            # One real Telegram bot has one explicit persona, not index rotation.
            timku = [{"nama": bot["nama"], "persona": bot.get("persona", "")}]
            if not timku:
                continue
            tekstl = text.lower()
            dipilih = next((p for p in timku if p["nama"].lower() in tekstl), None)
            if not dipilih:
                idx = giliran.get(kunci := substr(chat_id), 0)
                dipilih = timku[idx % len(timku)]
                giliran[kunci] = idx + 1
                    # py: even under turn rotation, each persona still keeps its own session
            if len(timku) > 1:
                disebut = disebut or dipilih["nama"].lower() in tekstl

            if re.search(r"batalkan jadwal", text, re.I):
                hapus = [s["id"] for s in STORE.list_schedules()
                         if int(s["chat_id"]) == int(chat_id) and s["bot_id"] == bot_id(bot)]
                for jd_id in hapus:
                    STORE.delete_schedule(jd_id)
                send_msg(bot["token"], chat_id, "🗑️ All schedules in this chat are cancelled.")
                continue
            m_ing = re.search(r"(?:ingetin|ingatkan|remind\w*)\s*(?:aku|kami|saya)?\s*(besok|lusa)?\s*[a-z ]{0,10}?(?:jam|pukul)\s*(\d{1,2})(?:[.:](\d{2}))?", text, re.I)
            if m_ing and (disebut or balik or private):
                ofst = {"besok": 1, "lusa": 2}.get((m_ing.group(1) or "").lower(), 0)
                jamc = f"{int(m_ing.group(2)) % 24:02d}:{(m_ing.group(3) or '00')[:2].zfill(2)}"
                expr, tampil, kapan = buat_one_shot(ofst, jamc)
                STORE.add_schedule(bot_id(bot), int(chat_id), expr, "once",
                                   f"One-shot reminder from the chat. Send a casual one-to-two-sentence reminder "
                                   f"using this context: {text[:200]}")
                send_msg(bot["token"], chat_id, f"✅ Okay! I'll remind you here at {tampil} WIB ({kapan}) 😄")
                continue
            m_jdw = re.search(r"jadwalkan.*?(?:jam|pukul)\s*(\d{1,2})(?:[.:](\d{2}))?", text, re.I)
            if m_jdw and (disebut or balik or private):
                jamc = f"{int(m_jdw.group(1)) % 24:02d}:{(m_jdw.group(2) or '00')[:2].zfill(2)}"
                STORE.add_schedule(bot_id(bot), int(chat_id), jamc, "cron",
                                   "Send a warm greeting message, casual style, max 2 sentences, end with an emoji.")
                send_msg(bot["token"], chat_id, f"✅ Okay! I'll greet here every day at {jamc} 😄")
                continue

            # 🤫 Default [SILENT] mode: in groups the bot only listens unless addressed.
            # Automatic initiative is handled by initiatif_loop — random triggers were removed (Phase 3).
            if not (disebut or balik or private):
                PESAN_BARU[int(chat_id)] = {"bot": bot, "ts": time.time()}
                continue
            last_cmd[str(chat_id)] = time.time()
            bot["last_speaker"] = dipilih["nama"]

            def proses(bot=bot, chat_id=chat_id, penulis=penulis, text=text,
                       persona=dipilih, banyak=len(timku), otonom=False):
                try:
                    bident = bot.get("bot_id") or bot["token"].split(":", 1)[0]
                    if METER.over_budget(bident):
                        log(f"💰 {persona['nama']} stopping: today's budget is full "
                            f"({METER.today(bident)}).")
                        return
                    if RATE and not RATE.allow(bident):
                        log(f"🚦 {persona['nama']} postponed: this minute's quota is full")
                        return
                    sid = sesi_untuk(bot, chat_id, persona)
                    ctx = CHATBUF.format_context(chat_id, cfg.get("trigger", {}).get("konteks_n", 15))
                    prompt = (f"[Recent conversation context (including messages you didn't answer)]:\n{ctx}\n\n"
                            f"[Message from {penulis}]: {text}")
                    if otonom:
                        prompt = ("[Context: you've been given your own initiative space. If this message isn't "
                                "worth an answer (boring chatter, you just spoke, or it would be intrusive), "
                                "answer with ONLY one word: SILENT. If it's worth it, answer normally.]\n" + prompt)
                    profiler = {"running": True}

                    def sedang_mengetik():
                        while profiler["running"]:
                            tg(bot["token"], "sendChatAction", {"chat_id": chat_id, "action": "typing"})
                            time.sleep(4)

                    threading.Thread(target=sedang_mengetik, daemon=True).start()
                    answer = oc_prompt(sid, prompt)
                    profiler["running"] = False
                    if answer:
                        CHATBUF.append(chat_id, "bot", answer, name=persona["nama"])
                        sisi = http(f"{API_URL}/api/session/{sid}", auth=AUTH)
                        _c, _t = METER.extract_and_record(bident, sisi.get("data") or {})
                        log(f"💰 meter: {_c:,.4f} USD, {_t} tokens used today, total {METER.today(bident)}")
                    profiler["running"] = False
                    log(f"🧠 {persona['nama']} reply:", (answer or "⚠️ FAILED")[:80])
                    if not answer:
                        return
                    if answer.strip().upper().startswith(("SILENT", "DIAM")):
                        log(f"🤫 {persona['nama']} chose silent (autonomous).")
                        return
                    if banyak > 1 and not cfg.get("otonomi"):
                        answer = f"💬 *{persona['nama']}*\n{answer}"
                    send_msg(bot["token"], chat_id, answer)
                except Exception as e:
                    log("❗ process error:", repr(e))

            otonom = False  # called directly → answer normally; the initiative space lives in initiatif_loop
            threading.Thread(target=proses, daemon=True).start()
        SHUTDOWN.wait(0.5)


def substr(x):
    return str(x)


def main():
    global token_ok
    try:
        import signal
        signal.signal(signal.SIGTERM, request_shutdown)
        signal.signal(signal.SIGINT, request_shutdown)
    except (ImportError, ValueError):
        pass
    if not cfg.get("owner_ids"):
        raise RuntimeError("owner_ids must be filled in before the bridge can run")
    with open(os.path.join(HERE, "bridge.pid"), "w") as handle:
        handle.write(str(os.getpid()))
    token_ok = [b for b in cfg["bots"]
                if not b.get("token", "").startswith("ISI") and len(b.get("token", "")) > 20]
    if not token_ok:
        print("⚠️  No bot token yet! Paste one into config.json and run again.")
        return
    migrasi_cron()  # cron.json → schedules table, once at startup
    pulih_chats_json()  # legacy chats.json → chats table
    sinkron_persona()   # personas.json ↔ personas table + mirror
    pulihkan_tugas_beku()
    threading.Thread(target=cron_loop, daemon=True).start()
    threading.Thread(target=initiatif_loop, daemon=True).start()
    threading.Thread(target=tugas_loop, daemon=True).start()
    threading.Thread(target=provision_loop, daemon=True).start()
    threading.Thread(target=outbox_loop, daemon=True).start()
    # 🧍 immunity: a thread dies → it is started again automatically
    threads = {}
    def nyalakan_ulang():
        for i, b in enumerate(token_ok):
            threads[i] = threading.Thread(target=run_bot, args=(b, i), daemon=True)
            threads[i].start()
    nyalakan_ulang()
    log(f"🚀 bridge running, {len(token_ok)} tokens, personas: {len(muat_persona())}")
    while not SHUTDOWN.is_set():
        SHUTDOWN.wait(30)
        for i, b in enumerate(token_ok):
            if not threads.get(i) or not threads[i].is_alive():
                log(f"🔧 bot-{i} thread died, restarting...")
                threads[i] = threading.Thread(target=run_bot, args=(b, i), daemon=True)
                threads[i].start()
    log("🛑 bridge stopped, all threads left to shut down on their own.")


if __name__ == "__main__":
    main()
