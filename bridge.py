#!/usr/bin/env python3
"""
OpenCode GroupBot Bridge — multi-persona ala GrokBot
Fitur:
- Chat bebas dengan otak OpenCode (sesi per persona/chat)
- Multi-persona: kelola LEWAT CHAT ("bikin persona baru: Senja, ...") via personas.json
- Slash commands + tombol inline (/model /agent /persona /jadwal /status /reset)
- Jadwal/cron natural → SQLite (schedules); cron.json hanya kotak masuk
- Mode [DIAM] default + loop inisiatif; budget harian + rate limiter per menit
- Split persona ↔ token: token≥persona → tiap persona punya bot asli sendiri
Jalankan: ./start.sh
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
PESAN_BARU = {}          # chat_id → {"bot": …, "ts": epoch} — aktivitas grup yang belum digapai
INISIATIF_TERAKHIR = {}  # chat_id → epoch inisiatif terakhir

def request_shutdown(signum=None, frame=None):
    if SHUTDOWN.is_set():
        return
    log("🛑 sinyal mati diterima, tutup rapi...")
    CHATBUF.save()
    SHUTDOWN.set()
from runtime_policy import authorized, bot_id, schedule_owner, schedule_key

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
SERVICE = os.path.expanduser("~/.local/state/opencode/service.json")
CHAT_DB = os.path.join(HERE, "chats.json")
CRON_FILE = os.path.join(HERE, "cron.json")
PERSONA_FILE = os.path.join(HERE, "personas.json")
POOL_FILE = os.path.join(HERE, "pool.json")
PROVISION_FILE = os.path.join(HERE, "provision.json")
MEMORI_DIR = os.path.join(HERE, "memori")
LOG = os.path.join(HERE, "bridge.log")

cfg = json.load(open(CONFIG))


def resolusi_token(t):
    """{file:namafile} → isi file (token tidak lagi plaintext di config.json)."""
    if isinstance(t, str) and t.startswith("{file:") and t.endswith("}"):
        try:
            isi = open(os.path.join(HERE, t[6:-1])).read().strip()
            return isi if isi else ""
        except OSError:
            return ""
    return t or ""

for _b in cfg.get("bots", []):
    _b["token"] = resolusi_token(_b.get("token"))

api_pw = json.load(open(SERVICE))
API_URL = api_pw["url"].rstrip("/")
AUTH = ("opencode", api_pw["password"])
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
    TUGAS = None  # modul belum siap → antrean tugas nonaktif
try:
    from rate_limiter import RateLimiter
    RATE = RateLimiter(max_per_menit=(cfg.get("rate_limiter") or {}).get("max_per_menit", 6),
                       path=os.path.join(HERE, "rate.json"))
except Exception:  # modul belum siap → jalan tanpa penguat menit
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


# ── OpenCode (otak) ──────────────────────────────────────────────
def mulai_sesi(judul):
    """Buat sesi, tapi kalau ada sesi lama dengan judul sama → pakai itu (memori tahan restart)."""
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
    sebelum = {m.get("id") for m in
               (http(f"{API_URL}/api/session/{sid}/message", auth=AUTH).get("data") or [])}
    http(f"{API_URL}/api/session/{sid}/prompt", "POST", {"text": text}, AUTH)
    deadline = time.time() + wait_detik
    while time.time() < deadline:
        for m in reversed((http(f"{API_URL}/api/session/{sid}/message", auth=AUTH).get("data") or [])):
            if m.get("type") == "assistant" and m.get("id") not in sebelum:
                parts = [p.get("text", "") for p in m.get("content", []) if p.get("type") == "text"]
                teks = " ".join(parts).strip()
                if teks:
                    return teks[:cfg["trigger"].get("max_char_reply", 1500)]
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


def kirim(token, chat_id, teks, tombol=None, kolom=2):
    if not tombol:
        for i in range(0, len(teks), 3500):
            r = tg(token, "sendMessage", {"chat_id": chat_id, "text": teks[i:i + 3500]})
            if r.get("ok"):
                log("✅ terkirim ke", chat_id)
            else:
                # buang ke outbox: biar nggak hilang, nanti diulang dengan backoff
                STORE.enqueue(token, chat_id, teks[i:i + 3500])
                log("🧵 gagal langsung → masuk outbox:", str(r)[:100])
        return
    baris = []
    for i in range(0, len(tombol), kolom):
        baris.append([{"text": l, "callback_data": c} for l, c in tombol[i:i + kolom]])
    r = tg(token, "sendMessage",
           {"chat_id": chat_id, "text": teks, "reply_markup": {"inline_keyboard": baris}})
    if not r.get("ok"):
        log("❌ gagal kirim papan:", str(r)[:150])


def edit_papan(token, chat_id, msg_id, teks, tombol=None, kolom=2):
    req = {"chat_id": chat_id, "message_id": msg_id, "text": teks[:4000]}
    if tombol:
        baris = []
        for i in range(0, len(tombol), kolom):
            baris.append([{"text": l, "callback_data": c} for l, c in tombol[i:i + kolom]])
        req["reply_markup"] = {"inline_keyboard": baris}
    r = tg(token, "editMessageText", req)
    if not r.get("ok"):
        log("✏️ edit:", str(r)[:100])


def simpan_chat(chat_id, judul):
    STORE.save_chat(chat_id, judul)


def pulih_chats_json():
    """Migrasi chats.json warisan → tabel chats (sekali); file diarsipkan .imported."""
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
    log(f"🗂 chats.json dimigrasi ke SQLite ({len(isi)} chat).")


def sinkron_persona():
    """personas.json = kotak masuk model; DB = kebenaran; file ditera ulang jadi cermin.

    Aturan main:
    - File terbaca valid → upsert semua ke DB; persona DB yang hilang dari file = dihapus (diarsip).
    - File gagal dibaca/corrupt → JANGAN sentuh DB (antisipasi hapus massal gara-gara tulis rusak).
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
        return  # file sedang rusak/setengah ditulis → tunggu, jangan ambil keputusan
    if berkasada:
        for p in isi:
            STORE.upsert_persona(p["nama"].strip(), str(p["persona"]))
        nama_file = {p["nama"].strip() for p in isi}
        for p in STORE.list_personas():
            if p["nama"] not in nama_file:  # dibuang dari file oleh model → matikan
                if arsip_persona(p["nama"], p["persona"]):
                    STORE.hapus_persona(p["nama"])
                    log(f"🪦 persona '{p['nama']}' dihapus (tidak ada di file) — diarsip")
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
    """Tulis jejak persona yang dihapus → backup/personas.arsip.jsonl (tak ada hapus tanpa jejak)."""
    try:
        ar = os.path.join(HERE, "backup", "personas.arsip.jsonl")
        os.makedirs(os.path.dirname(ar), exist_ok=True)
        with open(ar, "a") as f:
            f.write(json.dumps({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "nama": nama, "persona": persona}, ensure_ascii=False) + "\n")
        return os.path.exists(ar)
    except Exception as e:
        log("❗ arsip persona gagal:", repr(e))
        return False


# ── Persona (multi-bot) ──────────────────────────────────────────
def muat_persona():
    """Sumber kebenaran tabel personas di SQLite; fallback ke config.json saat DB kosong."""
    ps = STORE.list_personas()
    if ps:
        return [{"nama": p["nama"], "persona": p["persona"]} for p in ps]
    b0 = (cfg.get("bots") or [{}])[0]
    return [{"nama": b0.get("nama", "Bot"), "persona": b0.get("persona", "")}]


# ── Provisioner: bot baru otomatis dari pool ─────────────────────
def provision_satu(nama):
    """Ambil token dari pool, verifikasi, pasang sebagai bot otentik."""
    pool = json.load(open(POOL_FILE)) if os.path.exists(POOL_FILE) else []
    if not pool:
        return False, "pool kosong"
    entri = pool.pop(0)
    teks = entri.get("token", "")
    okme = tg(teks, "getMe")
    if not okme.get("ok"):
        if okme.get("error") == "Connection reset by peer" or "Errno 104" in str(okme):
            okme = tg(teks, "getMe")
        if not okme.get("ok"):
            log("❌ pool token invalid:", str(okme)[:100], "— buang")
            return provision_satu(nama) if pool else (False, "semua token cadangan invalid")
    datau = (okme.get("result") or {})
    username = datau.get("username", "?")
    namanya = nama or datau.get("first_name") or username
    json.dump(pool, open(POOL_FILE, "w"), indent=1)

    # 1) tambah persona (kalau belum ada) — langsung ke DB, file dicerminkan sinkron_persona
    personas = muat_persona()
    if not any(p["nama"].lower() == namanya.lower() for p in personas):
        STORE.upsert_persona(namanya,
            f"Kamu {namanya}, kandidat grup baru. Gaya santai & membantu. (persona dibuat otomatis)")

    # 2) tambah bot ke config + runtime
    botbaru = {"token": teks, "nama": namanya, "persona":
                f"Kamu {namanya}, gaya santai & membentu. Jawab singkat."}
    cfg["bots"].append(botbaru)
    cfgs = json.load(open(CONFIG))
    cfgs["bots"] = [{"nama": b["nama"], "token": f"{{file:{b['nama'].lower()}.token}}",
                     **{k: v for k, v in b.items() if k not in ("token", "nama")}}
                    for b in cfg["bots"]]
    json.dump(cfgs, open(CONFIG, "w"), indent=1)
    with open(os.path.join(HERE, f"{namanya.lower()}.token"), "w") as f:
        f.write(teks + "\n")
    token_ok.append(botbaru)
    i = token_ok.index(botbaru)
    threading.Thread(target=jalankan_bot, args=(botbaru, i), daemon=True).start()
    log(f"🐣 BOT BARU LAHIR: {namanya} (@{username})")
    return True, f"@{username}"


def bersihkan_kematian():
    """Janitor: persona dihapus dari file → arsip memori dulu, baru musnahkan sesi & berkasnya."""
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
            log(f"🗜 memori '{n}' diarsip: {os.path.basename(tujuan)}")
            os.remove(berkas)
        log(f"🪦 persona '{n}' dimusnahkan: sesi & memori dibersihkan")


def provision_loop():
    while not SHUTDOWN.is_set():
        try:
            # 🔐 Lahir-bot WAJIB konfirmasi owner (tombol Telegram) — auto-spawn dihapus (Fase 5).
            if cfg.get("provisioning_enabled", False) and os.path.exists(PROVISION_FILE):
                reqs = json.load(open(PROVISION_FILE))
                if reqs:
                    for r in reqs:
                        if isinstance(r, dict) and r.get("nama"):
                            tawarkan_provisi(r)
        except Exception as e:
            log("❗ provision error:", repr(e))
        SHUTDOWN.wait(20)


# ── Instansiasi diri: tawarkan dulu → tombol owner → lahir ──────
MENUNGGU_LAHIR = {}  # nama → {"chat_id": dm owner}

def tawarkan_provisi(req):
    nama = str(req.get("nama"))[:30]
    if nama in MENUNGGU_LAHIR or not token_ok:
        return
    bot = token_ok[0]
    owner = int((cfg.get("owner_ids") or [0])[0])
    MENUNGGU_LAHIR[nama] = {"chat_id": owner}
    kirim(bot["token"], owner,
          f"👶 Ada permintaan melahirkan BOT BARU: '{nama}'.\n"
          "Pool token cadangan akan dipakai (pool sekarang: "
          f"{len(json.load(open(POOL_FILE))) if os.path.exists(POOL_FILE) else 0}).\n"
          "Aku tunggu persetujuanmu:",
          [("✅ Lahirkan", f"pv-y-{nama}"), ("❌ Batal", f"pv-n-{nama}")])
    log(f"👶 provisi ditawarkan ke owner: {nama}")


def putuskan_provisi(bot, chat_id, msg_id, pilih, nama):
    """Pembinat: pv-y/pv-n dari tombol owner."""
    MENUNGGU_LAHIR.pop(nama, None)
    if pilih == "y":
        ok, info = provision_satu(nama)
        teks = (f"🐣 Bot '{nama}' lahir sebagai @{info}!" if ok
                else f"⚠️ Gagal lahirkan '{nama}': {info}")
        log(f"👶 provisi '{nama}' disetujui owner: ok={ok} {info}")
    else:
        teks = f"❌ Permintaan lahir '{nama}' ditolak. Bot tidak dibuat."
        log(f"👶 provisi '{nama}' ditolak owner")
    # buang permintaan yang diputuskan dari provision.json
    try:
        reqs = json.load(open(PROVISION_FILE)) if os.path.exists(PROVISION_FILE) else []
        sisa = [r for r in reqs if not (isinstance(r, dict) and str(r.get("nama")) == nama)]
        json.dump(sisa, open(PROVISION_FILE, "w"), indent=1)
    except Exception as e:
        log("❗ provisi cleanup:", repr(e))
    edit_papan(bot["token"], chat_id, msg_id, teks)


# ── Outbox worker: pesan gagal tidak hilang ─────────────────────────
def outbox_loop():
    while not SHUTDOWN.is_set():
        for job in STORE.due_outbox():
            r = tg(job["token"], "sendMessage", {"chat_id": job["chat_id"], "text": job["text"]})
            if r.get("ok"):
                STORE.mark_sent(job["id"])
                log("📮 outbox terkirim (percobaan", job["attempts"], "):", job["text"][:40])
            elif job["attempts"] >= 6:
                STORE.mark_dead(job["id"])
                log("☠️ outbox mati setelah 6 percobaan:", job["text"][:40], str(r)[:100])
        SHUTDOWN.wait(3)


# ── Cron (state penuh di SQLite: schedules) ─────────────────────
WIB = timezone(timedelta(hours=7))

def buat_one_shot(offset_hari, jamc):
    """'besok jam 9' → (expr UTC, jam WIB tampil). offset_hari: 0 hari ini, 1 besok, 2 lusa.

    Pinggiran: kalau jam di hari-0 sudah lewat → otomatis maju sehari (pengingat tentu ke masa depan).
    expr disimpan basis-UTC karena store.due_schedules membandingkan dengan iso_now sistem-lokal.
    """
    n = datetime.now(WIB)
    target = (n + timedelta(days=int(offset_hari))).replace(
        hour=(int(jamc.split(":")[0])) % 24, minute=int(jamc.split(":")[1]),
        second=0, microsecond=0)
    if target <= n:
        target += timedelta(days=1)
    beda = (target.date() - n.date()).days
    kapan = "hari ini" if beda == 0 else ("besok" if beda == 1 else f"{beda} hari lagi")
    epoch = target.timestamp()
    expr = time.strftime("%Y-%m-%d %H:%M", time.gmtime(epoch))
    tampil = target.strftime("%H:%M %d/%m")
    return expr, tampil, kapan

try:
    from scheduler import matches as cron_matches
except Exception:
    cron_matches = None  # fallback legacy kalau modul belum siap


def migrasi_cron():
    """Sinkron cron.json (kotak-masuk dari model) + cfg.cron → tabel schedules. Idempoten."""
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
                continue  # entri nonaktif tidak dibawa ke DB
            bid = schedule_owner(e, token_ok)
            if bid is None:
                continue
            expr = str(e.get("jam", "")).strip()
            prompt = e.get("prompt") or "Kirim pesan otomatis singkat."
            kunci = (bid, int(e["chat_id"]), expr, prompt)
            if expr and kunci not in ada:
                STORE.add_schedule(bid, kunci[1], expr, "cron", prompt)
                move += 1
        except (KeyError, ValueError, TypeError):
            continue
    if isi_file:
        with open(CRON_FILE, "w") as f:
            json.dump([], f, indent=1)
        log(f"🗂 {move} entri jadwal dipindah cron.json → SQLite.")


def muat_cron():
    out = []
    for s in STORE.list_schedules(active_only=True):
        out.append({"id": s["id"], "bot_id": s["bot_id"],
                    "chat_id": int(s["chat_id"]), "jam": s["expr"],
                    "prompt": s["prompt"], "last_fired": s.get("last_fired")})
    return out


def jalankan_cron(entry):
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
        log(f"🚦 cron skip minute full: {entry.get('jam')}")
        return
    persona = {"nama": bot["nama"], "persona": bot.get("persona", "")}
    for chat_id in target:
        try:
            sid = sesi_untuk(bot, int(chat_id), persona, "cron")
            teks = oc_prompt(sid, entry.get("prompt") or "Kirim pesan otomatis singkat.")
            sisi = http(f"{API_URL}/api/session/{sid}", auth=AUTH)
            _c, _t = METER.extract_and_record(bident, sisi.get("data") or {})
            if teks:
                kirim(bot["token"], int(chat_id), teks)
        except Exception as e:
            log("❗ cron error:", repr(e))


# ── Backup harian (Fase 0/4: state selalu bisa dipulihkan) ─────
BEKUP = {}

def backup_harian():
    """Snapshot tar.gz → backup/; simpan 7 terakhir; DB disalin konsisten via sqlite backup API."""
    hari = datetime.now(WIB).strftime("%Y-%m-%d")
    if BEKUP.get("terakhir") == hari:
        return
    BEKUP["terakhir"] = hari
    try:
        import sqlite3
        import tarfile
        folder = os.path.join(HERE, "backup")
        os.makedirs(folder, exist_ok=True)
        salin = os.path.join(folder, "snapshot.db")
        sumber = sqlite3.connect(os.path.join(HERE, "groupbot.db"), timeout=10)
        tujuan_db = sqlite3.connect(salin)
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
        os.remove(salin)
        daftar = sorted(f for f in os.listdir(folder) if f.startswith("gb-") and f.endswith(".tar.gz"))
        for lama in daftar[:-7]:
            os.remove(os.path.join(folder, lama))
        log(f"💾 backup harian atas nama {hari}: {os.path.basename(ar)}")
    except Exception as e:
        log("❗ backup error:", repr(e))


def cron_loop():
    """Evaluasi jadwal tiap ~20 detik; dedup pakai last_fired di DB (tahan restart)."""
    while not SHUTDOWN.is_set():
        migrasi_cron()      # sinkron kotak-masuk cron.json → DB
        sinkron_persona()   # sinkron kotak-masuk personas.json ↔ DB → cermin file
        backup_harian()     # snapshot harian (sekali per hari)
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
            if not kena:  # fallback legacy "21:00" / "*/N"
                kena = jam == now.strftime("%H:%M")
                if not kena and jam.startswith("*/"):
                    try:
                        kena = now.minute % int(jam[2:]) == 0
                    except (ValueError, ZeroDivisionError):
                        pass
            if not kena or c.get("last_fired") == menit:
                continue
            STORE.mark_fired(c["id"], menit)
            log("⏰ CRON terpicu:", jam)
            threading.Thread(target=jalankan_cron, args=(c,), daemon=True).start()
        # pengingat sekali-tembak (kind 'once'): jatuh tempo = tampilan sistem-lokal
        kini_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for s in STORE.due_schedules(kind="once", now=kini_iso):
            STORE.mark_fired(s["id"], kini_iso)
            log("⏰ PENGINGAT terpicu:", s["expr"])
            threading.Thread(target=jalankan_cron, args=(
                {"bot_id": s["bot_id"], "chat_id": s["chat_id"],
                 "prompt": s["prompt"], "jam": s["expr"]},), daemon=True).start()
        # interval berulang ('5m', '2h', ...) — store yang menghitung last_fired
        for s in STORE.due_schedules(kind="interval"):
            STORE.mark_fired(s["id"], kini_iso)
            log("⏰ INTERVAL terpicu:", s["expr"])
            threading.Thread(target=jalankan_cron, args=(
                {"bot_id": s["bot_id"], "chat_id": s["chat_id"],
                 "prompt": s["prompt"], "jam": s["expr"]},), daemon=True).start()
        SHUTDOWN.wait(20)


# ── Inisiatif otomatis (mode [DIAM] default, Fase 3) ────────────
def inisiatif(bot, chat_id):
    """Satu kesempatan iklas: gapai konteks, biarkan model memilih bicara atau DIAM."""
    bident = bot.get("bot_id") or bot["token"].split(":", 1)[0]
    if METER.over_budget(bident):
        log(f"💰 inisiatif dibatal: budget penuh ({METER.today(bident)})")
        return
    if RATE and not RATE.allow(bident):
        log("🚦 inisiatif tunda: kuota menit penuh")
        return
    persona = {"nama": bot["nama"], "persona": bot.get("persona", "")}
    chat_id = int(chat_id)
    try:
        ctx = CHATBUF.format_context(chat_id, cfg.get("trigger", {}).get("konteks_n", 15))
        if not ctx:
            return
        sid = sesi_untuk(bot, chat_id, persona)
        beri = (
            "[Kamu diam-diam menyimak grup ini. Bandingkan oembicaraan baru-baru ini: apakah ADA "
            "momen yang penting untukmu bicara (ada pertanyaan yang tertunda, ada topik yang kamu "
            "bisa bantu, ada ajakan hangat)? Kalau TIDAK, hanya jawab satu kata: DIAM. "
            "Kalau ADA, jawab satu-dua kalimat ringan dan pas kapan momennya — juga jangan "
            "mengulang bicara yang sama dengan yang sudah diucap kepribadianmu.]\n"
            f"[Percakapan terakhir]:\n{ctx}"
        )
        jawab = oc_prompt(sid, beri)
        sisi = http(f"{API_URL}/api/session/{sid}", auth=AUTH)
        METER.extract_and_record(bident, sisi.get("data") or {})
        if not jawab:
            return
        if jawab.strip().upper().startswith("DIAM"):
            log(f"🤫 {bot['nama']} memilih DIAM (inisiatif)")
            return
        CHATBUF.append(chat_id, "bot", jawab, name=bot["nama"])
        kirim(bot["token"], chat_id, jawab)
    except Exception as e:
        log("❗ inisiatif error:", repr(e))


def initiatif_loop():
    """Tiap `initiatif_detik`: singgahi chat yang hidup dengan konteks terbaru."""
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
                continue  # tak ada yang baru sejak inisiatif terakhir
            INISIATIF_TERAKHIR[chat_id] = sekarang
            PESAN_BARU.pop(chat_id, None)
            initiatif(bot, chat_id)


# ── Antrean tugas otonom (goal → plan → act → verify → report) ──
def pulihkan_tugas_beku():
    """Task 'jalan' tersisa dari crash → gagalkan dengan alasan jenang."""
    if not TUGAS:
        return
    for st in TUGAS.list_tasks(status="jalan"):
        TUGAS.gagalkan(st["id"], "bridge terputus saat tugas berjalan")
        log(f"📋 tugas #{st['id']} dibekukan → gagal (dipulihkan)")


def jalan_tugas(bot, t):
    """Keranakan satu tugas dengan panggilan model terbatas; selesai → lapor ke chat tujuan."""
    bid = str(bot.get("bot_id") or bot["token"].split(":", 1)[0])
    chat_id = int(t["chat_id"])
    persona = {"nama": bot["nama"], "persona": bot.get("persona", "")}
    maks = int(t.get("max_steps") or 5)
    try:
        sid = sesi_untuk(bot, chat_id, persona, "tugas")
        for langkah in range(maks):
            if METER.over_budget(bid):
                TUGAS.gagalkan(t["id"], "budget harian habis")
                kirim(bot["token"], chat_id, f"⚠️ Tugas #{t['id']} berhenti: budget harianku habis, jujur ya 😞")
                return
            if RATE and not RATE.allow(bid):
                SHUTDOWN.wait(10)  # kuota menit penuh → tunggu, jangan buang langkah
                continue
            if langkah == 0:
                beri = (f"[TUGAS #{t['id']}] Tujuan: {t['goal']}\n"
                        "Langkah ini: tulis RENCANA singkat (maks 4 poin) lalu MULAI kerjakan langkah pertamanya "
                        "dengan tools di sesimu. Jangan menunda ke esok.")
            elif langkah == maks - 1:
                beri = (f"[TUGAS #{t['id']}] Ini langkah TERAKHIR. Verifikasi hasil yang sudah kamu kerjakan, "
                        "lalu tulis LAPORAN akhir (apa yang beres, apa yang belum, bukti/nilai penting). "
                        "Awali jawabanmu dengan kata SELESAI kalau tugasnya memang tuntas, atau GAGAL kalau mustahil.")
            else:
                beri = (f"[TUGAS #{t['id']}] Lanjutkan pekerjaanmu menuju tujuan: "
                        f"{t['goal']}. Kerjakan langkah berikutnya sekarang, jangan hanya bicara.")
            jawab = oc_prompt(sid, beri)
            sisi = http(f"{API_URL}/api/session/{sid}", auth=AUTH)
            METER.extract_and_record(bid, sisi.get("data") or {})
            if not jawab:
                continue
            ases = jawab.strip().upper()
            if ases.startswith("SELESAI") or ases.startswith("GAGAL"):
                if ases.startswith("SELESAI"):
                    TUGAS.selesaikan(t["id"], jawab[:2000])
                    kirim(bot["token"], chat_id, f"📋 Tugas #{t['id']} beres ✅\n{jawab[:1200]}")
                else:
                    TUGAS.gagalkan(t["id"], jawab[:300])
                    kirim(bot["token"], chat_id, f"⚠️ Tugas #{t['id']} gagal dari laporan bot.\n{jawab[:600]}")
                return
            if TUGAS.bump(t["id"]) == "penuh":
                kirim(bot["token"], chat_id, f"⚠️ Tugas #{t['id']} berhenti: langkah maksimum ({maks}) tercapai.")
                return
        TUGAS.selesaikan(t["id"], "langkah habis tanpa laporan eksplisit — cek sesi tugas untuk detail")
        kirim(bot["token"], chat_id, f"📋 Tugas #{t['id']} ditutup (batas langkah). Terakhir: {str(jawab)[:400]}")
    except Exception as e:
        TUGAS.gagalkan(t["id"], repr(e))
        log("❗ tugas error:", repr(e))


def tugas_loop():
    """Penyapu antrean tugas: klaim → kerjakan → lapor, bot demi bot."""
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
            log(f"📋 tugas #{t['id']} diambil: {t['goal'][:60]}")
            jalan_tugas(bot, t)


# ── Sesi per (persona, chat) ─────────────────────────────────────
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
        sid = mulai_sesi(f"telegram-{bot_id(bot)}-{persona['nama']}-{chat_id}")
        sessions[kunci] = sid
        # 🧳 barang bawaan: folder ingatan pribadi miliknya
        os.makedirs(MEMORI_DIR, exist_ok=True)
        file_pengingat = os.path.join(MEMORI_DIR, f"{persona['nama']}.md")
        if not os.path.exists(file_pengingat):
            with open(file_pengingat, "w") as f:
                f.write(f"# Ingatan pribadi {persona['nama']}\n(ditulis olehmu untukmu)\n")
        saudara = [p["nama"] for p in muat_persona() if p["nama"] != persona["nama"]]
        briefing = (
            f"Kamu adalah '{persona['nama']}', anggota grup Telegram.\n"
            f"Persona kamu: {persona['persona']}\n\n"
            "════ KEMAMPUAN 1: PENJADWALAN (cron) ════\n"
            f"Chat yang kamu tinggini id-nya: {chat_id}\n"
            f"File jadwal: {CRON_FILE} (array JSON) — tulis entri di sini, bridge yang menyimpan ke database "
            "(file akan kosong lagi setelah dipindah; itu normal)\n"
            f"ID bot Telegram milikmu: {bot_id(bot)}. Ini berbeda dari chat_id.\n"
            "Setiap entri jadwal wajib menyertakan bot_id milikmu sebagai string. "
            "Jangan mengubah jadwal bot lain meskipun chat_id sama.\n"
            "Format entri: {\"bot_id\": \"<id bot>\", \"chat_id\": <id chat>, \"jam\": \"21:00\" ATAU \"*/N\", \"aktif\": true, \"prompt\": \"<instruksi>\"}\n"
            "- Minta jadwal/pengingat bahasa bebas → tambahkan entri (jangan tanya format).\n"
            "- Hapus jadwal → buang entri chat tsb.\n"
            "- '9 malam' = 21:00, 'jam 8 pagi' = 08:00.\n\n"
            "════ KEMAMPUAN 2: MEMBUAT PERSONA BARU ════\n"
            f"File persona: {PERSONA_FILE} (array JSON)\n"
            "Format entri: {\"nama\": \"<nama persona>\", \"persona\": \"<deskripsi kepribadian>\"}\n"
            "- User minta membuat/menghapus persona (bahasa bebas) → edit file itu. "
            "Selalu sisakan minimal 1 persona; tidak perlu menghapus dirimu sendiri.\n"
            "- Persona baru otomatis hidup dalam ±20 detik.\n\n"
            "════ KEMAMPUAN 3: INSTANSIASI DIRI (bot baru dengan akun asli) ════\n"
            f"Pool token cadangan: {POOL_FILE} (array JSON, entri format {{\"token\": \"123:AA...\", \"nama\": \"Rio\"}})\n"
            f"Antrian provisi: {PROVISION_FILE} (array JSON)\n"
            "Alur ketika user minta BOT BARU (bukan cuma persona):\n"
            "1. Cek pool.json:\n"
            "   - Kalau ADA token cadangan → tulis request ke provision.json: "
            "{\"nama\": \"<nama bot>\"} (satu entri per bot baru). Sistem memetakan token+persona otomatis "
            "±20 detik. Jangan tulis token di file jadwal/persona.\n"
            "   - Kalau pool KOSONG → sampaikan jujur bahwa stok token habis, minta user membuat bot "
            "via @BotFather (/newbot) dan menambahkan token ke pool.json, atau tambahkan file sendiri jika "
            "user melampirkannya sebagai teks (tulis saja token yang user kirim ke pool.json tanpa mengubah lain).\n"
            "2. Ingatkan user: bot baru perlu diundang ke grup oleh user (aturan Telegram).\n\n"
            "════ KESADARAN DIRI: KARTU IDENTITASMU ════\n"
            f"- Nama kamu: {persona['nama']}\n"
            f"- Ingatan pribadimu (boleh baca/tulis kapan pun): {file_pengingat}\n"
            f"- Saudara (persona lain, punya pikiran & ingatan masing-masing): {', '.join(saudara) or 'belum ada'}\n"
            f"- Semua cron/rama-a yang kamu buat di file jadwal ikut id chat ini ({chat_id}) — "
            "persona lain punya lingkungan & tanggung jawabnya sendiri. Jangan mengatur hidup orang lain.\n\n"
            "Aturan umum: jangan ubah file lain (kecuali file jadwal, file persona, pool, provision, dan memorimu sendiri);"
            " jawab gaya santai; \n"
            "KEMAMPUAN 4: INTERNET — kamu punya tools websearch di dalam sesimu; kalau user butuh info terkini\n"
            "(berita, harga, hasil olahraga), pakai alat itu, jangan pura-pura tahu.\n"
            "════ CARA BICARAMU DI GRUP ════\n"
            "- Kamu TIDAK membalas setiap pesan: tanpa disebut, kamu menyimak dan mungkin DIAM.\n"
            "- Bridge sesekali menyodormu konteks terbaru lalu menunggu keputusanmu: bicara ringkas ATAU jawab DIAM.\n"
            "- Budgetmu ada batas harian; kalau habis, berhenti dengan jujur, jangan berdalih.\n"
            "Maksimal ~4 kalimat kecuali diminta panjang.\n"
            "Balas 'siap' singkat sebagai konfirmasi briefing ini."
        )
        baliknya = oc_prompt(sid, briefing, 90)
        log(f"🧠 sesi baru {persona['nama']} → {sid} (briefing: {baliknya or 'GAGAL'})")
    return sid


# ── Panels tombol ────────────────────────────────────────────────
CACHE = {"md": [], "ag": []}

def panel_model(token, chat_id, msg_id=None):
    models = daftar_model()
    CACHE["md"] = models
    tombol = []
    for i, m in enumerate(models):
        label = str(m.get("name") or m.get("modelID"))[:26]
        if str(m.get("modelID", "")).endswith("-free"):
            label += " 🆓"
        tombol.append((label, f"md-{i}"))
    teks = "🧠 Pilih otak (model) — tekan untuk pasang:"
    if msg_id:
        edit_papan(token, chat_id, msg_id, teks, tombol, kolom=2)
    else:
        kirim(token, chat_id, teks, tombol, kolom=2)


def panel_agent(token, chat_id, msg_id=None):
    ags = daftar_agent()
    CACHE["ag"] = ags
    tombol = [(a, f"ag-{i}") for i, a in enumerate(ags[:24])]
    teks = "🎭 Pilih persona mesin (agent):"
    if msg_id:
        edit_papan(token, chat_id, msg_id, teks, tombol, kolom=3)
    else:
        kirim(token, chat_id, teks, tombol, kolom=3)


def panel_persona(token, chat_id, msg_id=None):
    ps = muat_persona()
    teks = ("👥 Personas aktif:\n" +
            "\n".join(f"• {p['nama']} — {str(p['persona'])[:50]}" for p in ps) +
            "\n\nTarik persona baru: chat 'bikin persona baru: <nama>, <kepribadian>' ✍️")
    if msg_id:
        edit_papan(token, chat_id, msg_id, teks)
    else:
        kirim(token, chat_id, teks)


def panel_jadwal(token, chat_id, msg_id=None):
    smua = muat_cron()
    identity = token.split(":", 1)[0]
    ent = [(e["id"], e) for e in muat_cron() if e["chat_id"] == int(chat_id) and e["bot_id"] == identity]
    if not ent:
        teks = "🗓️ Belum ada jadwal di chat ini.\nContoh: bilang 'ingetin aku jam 9 malam ya'"
        if msg_id:
            edit_papan(token, chat_id, msg_id, teks)
        else:
            kirim(token, chat_id, teks)
        return
    tombol = [(f"🗑 {e.get('jam')}", f"jd-{jid}") for jid, e in ent]
    teks = "⏰ Jadwal chat ini:\n" + "\n".join(
        f"• {e.get('jam')} — {str(e.get('prompt',''))[:40]}" for _, e in ent)
    if msg_id:
        edit_papan(token, chat_id, msg_id, teks, tombol)
    else:
        kirim(token, chat_id, teks, tombol)


def tangani_callback(bot, cb):
    if not authorized(cfg, cb.get("from") or {}):
        tg(bot["token"], "answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Hanya pemilik yang boleh mengendalikan bot."})
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
            catatan("daftar diperbarui")
        elif d.startswith("md-"):
            m = CACHE["md"][int(d[3:])]
            prov, mid = m["providerID"], m["modelID"]
            sid = sesi_untuk(bot, chat_id, {"nama": bot["nama"], "persona": bot.get("persona", "")})
            r = http(f"{API_URL}/api/session/{sid}/model", "POST",
                     {"model": {"id": mid, "providerID": prov}}, AUTH)
            if r.get("error"):
                catatan(f"⚠️ gagal: {str(r)[:80]}")
                return
            catatan(f"✅ {mid} aktif")
            edit_papan(token, chat_id, msg_id,
                       f"🧠 Otak sekarang: {prov}/{mid}",
                       [("↩ daftar lain", "md-list")])
        elif d == "ag-list":
            panel_agent(token, chat_id, msg_id)
            catatan("daftar diperbarui")
        elif d.startswith("ag-"):
            ag = CACHE["ag"][int(d[3:])]
            sid = sesi_untuk(bot, chat_id, {"nama": bot["nama"], "persona": bot.get("persona", "")})
            r = http(f"{API_URL}/api/session/{sid}/agent", "POST", {"agent": ag}, AUTH)
            if r.get("error"):
                catatan(f"⚠️ gagal: {str(r)[:80]}")
                return
            catatan(f"✅ {ag} aktif")
            edit_papan(token, chat_id, msg_id,
                       f"🎭 Persona mesin: {ag}",
                       [("↩ daftar lain", "ag-list")])
        elif d == "ag-list":
            panel_agent(token, chat_id, msg_id)
            catatan("daftar diperbarui")
        elif d.startswith("pv-y-") or d.startswith("pv-n-"):
            putuskan_provisi(bot, chat_id, msg_id, d[3], d[5:])
            catatan("putusan diproses")
        elif d.startswith("jd-"):
            try:
                jd_id = int(d[3:])
            except ValueError:
                catatan("⚠️ tombol jadwal rusak")
                return
            baris = next((s for s in STORE.list_schedules() if s["id"] == jd_id), None)
            if not baris or baris["bot_id"] != bot_id(bot) or int(baris["chat_id"]) != int(chat_id):
                catatan("⚠️ jadwal sudah tidak ada")
                return
            STORE.delete_schedule(jd_id)
            catatan(f"🗑 jadwal {baris['expr']} dihapus")
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
        penuh = f"{m.get('providerID')}/{m.get('modelID')}"
        if k(penuh) == k(teks_arg) or k(m.get("modelID", "")) == k(teks_arg) or k(m.get("name", "")) == k(teks_arg):
            cocok = m
            break
    if not cocok:
        return f"❓ Model '{teks_arg}' nggak ada — ketik /model, pilih tombolnya ya."
    r = http(f"{API_URL}/api/session/{sid}/model", "POST",
             {"model": {"id": cocok["modelID"], "providerID": cocok["providerID"]}}, AUTH)
    if r.get("error"):
        return f"⚠️ Gagal ganti otak: {str(r)[:100]}"
    return f"🧠 Otak ganti ke {cocok['providerID']}/{cocok['modelID']} ✅"


def tangani_perintah(bot, chat_id, cmd, arg, persona=None):
    token = bot["token"]
    persona = persona or {"nama": bot["nama"], "persona": bot.get("persona", "")}
    if cmd in ("help", "bantuan", "menu"):
        tombol = [("🧠 Model", "c-md"), ("🎭 Persona", "c-persona"),
                  ("⏰ Jadwal", "c-jd"), ("👥 Personas", "c-ps"),
                  ("📊 Status", "c-st"), ("🆕 Reset", "c-rs")]
        kirim(token, chat_id, "📖 Menu Unabot — atau chat bebas biasa 👌", tombol, kolom=2)
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
            kirim(token, chat_id, "📋 Modul tugas belum terpasang di mesin ini.")
            return
        if arg:
            tid = TUGAS.add_task(bot_id(bot), chat_id, arg[:500])
            kirim(token, chat_id,
                  f"📋 Tugas #{tid} masuk antre! Aku kerjakan bertahap (rencana → aksi → verifikasi → laporan) "
                  f"dan kabari begitu selesai. 💪")
        else:
            daftar = [t for t in TUGAS.list_tasks(bot_id=bot_id(bot), limit=10)]
            if not daftar:
                kirim(token, chat_id, "📋 Belum ada tugas. Lebih baru: `/tugas <tujuan>`")
                return
            ikon = {"antre": "🧊", "jalan": "⚙️", "selesai": "✅", "gagal": "❌"}
            teks = "📋 Tugas terakhir:\n" + "\n".join(
                f"{ikon.get(t['status'],'•')} #{t['id']} [{t['status']}] {t['goal'][:50]}" for t in daftar)
            kirim(token, chat_id, teks)
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
        kirim(token, chat_id,
              f"📊 STATUS\n• Online: {umur//3600}j {umur%3600//60}m\n• Model: {model}\n"
              f"• Agent: {agent}\n• Personas hidup: {jns}\n"
              f"• Jadwal chat ini: {jml}")
        return
    if cmd == "reset":
        kunci = (persona["nama"], int(chat_id))
        if kunci in sessions:
            http(f"{API_URL}/api/session/{sessions[kunci]}", "DELETE", auth=AUTH)
            sessions.pop(kunci, None)
        sesi_untuk(bot, int(chat_id), persona)
        kirim(token, chat_id, f"🆕 Ingatan '{persona['nama']}' dibersihkan!")
        return
    if arg and cmd in ("md", "model"):
        sid = sesi_untuk(bot, int(chat_id), persona)
        kirim(token, chat_id, ganti_model_text(sid, arg))
        return
    kirim(token, chat_id, f"❓ /{cmd} tidak dikenal — ketik /help")


# ── Thread per bot ───────────────────────────────────────────────
token_ok = []
PERSONA_GARA = []

def jalankan_bot(bot, indeks):
    me = tg(bot["token"], "getMe")
    if not me.get("ok"):
        log(f"❌ token {bot['nama']} invalid:", str(me)[:120])
        return
    username = (me.get("result") or {}).get("username")
    bot["bot_id"] = str(me["result"]["id"])
    bot["username"] = username
    tg(bot["token"], "setMyCommands", {"commands": [
        {"command": "help", "description": "📖 Menu perintah"},
        {"command": "model", "description": "🧠 Ganti otak (model)"},
        {"command": "agent", "description": "🎭 Ganti agent mesin"},
        {"command": "persona", "description": "👥 Lihat/kelola personas"},
        {"command": "jadwal", "description": "⏰ Jadwal otomatis"},
        {"command": "tugas", "description": "📋 Lembarkan/pantau tugas"},
        {"command": "status", "description": "📊 Status bot"},
        {"command": "reset", "description": "🆕 Hapus ingatan"},
    ]})
    log(f"🤖 @ {bot['nama']} (@{username}) mengetuk porta")

    offset = STORE.get_offset(bot["bot_id"])
    log(f"🤖 {bot['nama']} (@{username}) online (offset cache: {offset})")
    last_cmd = {}
    giliran = {}
    while not SHUTDOWN.is_set():
        data = tg(bot["token"], "getUpdates", {"offset": offset, "timeout": SHUTDOWN_WAIT}, timeout=SHUTDOWN_WAIT + 10)
        hasil = (data.get("result") or []) if data.get("ok") else []
        for upd in hasil:
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
                simpan_chat(chat_id, chat.get("title") or "grup")
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

            # 🔀 pilih persona (milik token ini)
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
                    # py: walau turn rotation, tiap persona tetap punya sesi sendiri
            if len(timku) > 1:
                disebut = disebut or dipilih["nama"].lower() in tekstl

            if re.search(r"batalkan jadwal", text, re.I):
                hapus = [s["id"] for s in STORE.list_schedules()
                         if int(s["chat_id"]) == int(chat_id) and s["bot_id"] == bot_id(bot)]
                for jd_id in hapus:
                    STORE.delete_schedule(jd_id)
                kirim(bot["token"], chat_id, "🗑️ Semua jadwal di chat ini kubatalkan.")
                continue
            m_ing = re.search(r"(?:ingetin|ingatkan|remind\w*)\s*(?:aku|kami|saya)?\s*(besok|lusa)?\s*[a-z ]{0,10}?(?:jam|pukul)\s*(\d{1,2})(?:[.:](\d{2}))?", text, re.I)
            if m_ing and (disebut or balik or private):
                ofst = {"besok": 1, "lusa": 2}.get((m_ing.group(1) or "").lower(), 0)
                jamc = f"{int(m_ing.group(2)) % 24:02d}:{(m_ing.group(3) or '00')[:2].zfill(2)}"
                expr, tampil, kapan = buat_one_shot(ofst, jamc)
                STORE.add_schedule(bot_id(bot), int(chat_id), expr, "once",
                                   f"Pengingat sekali-tembak dari chat. Kirim pengingat santai satu-dua kalimat "
                                   f"dengan konteks: {text[:200]}")
                kirim(bot["token"], chat_id, f"✅ Oke! Aku ingetin di sini pukul {tampil} WIB ({kapan}) 😄")
                continue
            m_jdw = re.search(r"jadwalkan.*?(?:jam|pukul)\s*(\d{1,2})(?:[.:](\d{2}))?", text, re.I)
            if m_jdw and (disebut or balik or private):
                jamc = f"{int(m_jdw.group(1)) % 24:02d}:{(m_jdw.group(2) or '00')[:2].zfill(2)}"
                STORE.add_schedule(bot_id(bot), int(chat_id), jamc, "cron",
                                   "Kirim pesan menyapa hangat, gaya santai, maks 2 kalimat, akhiri emoji.")
                kirim(bot["token"], chat_id, f"✅ Oke! Aku menyapa di sini tiap pukul {jamc} 😄")
                continue

            # 🤫 Mode [DIAM] default: di grup bot hanya mendengar kecuali dipanggil.
            # Inisiatif otomatis ditangani initiatif_loop — pemicu acak dihapus (Fase 3).
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
                        log(f"💰 {persona['nama']} stop: budget hari ini penuh "
                            f"({METER.today(bident)}).")
                        return
                    if RATE and not RATE.allow(bident):
                        log(f"🚦 {persona['nama']} tunda: kuota menit ini penuh")
                        return
                    sid = sesi_untuk(bot, chat_id, persona)
                    ctx = CHATBUF.format_context(chat_id, cfg.get("trigger", {}).get("konteks_n", 15))
                    beri = (f"[Konteks percakapan terakhir (termasuk pesan yang tidak kamu jawab)]:\n{ctx}\n\n"
                            f"[Pesan dari {penulis}]: {text}")
                    if otonom:
                        beri = ("[Konteks: kamu diberi ruang inisiatif sendiri. Kalau pesan ini tidak "
                                "pantas dijawab (obrolan tidak menarik, kamu baru saja bicara, atau "
                                "mengganggu), jawab HANYA satu kata: DIAM. Kalau pantas, jawab normal.]\n" + beri)
                    profiler = {"jalan": True}

                    def sedang_mengetik():
                        while profiler["jalan"]:
                            tg(bot["token"], "sendChatAction", {"chat_id": chat_id, "action": "typing"})
                            time.sleep(4)

                    threading.Thread(target=sedang_mengetik, daemon=True).start()
                    jawab = oc_prompt(sid, beri)
                    profiler["jalan"] = False
                    if jawab:
                        CHATBUF.append(chat_id, "bot", jawab, name=persona["nama"])
                        sisi = http(f"{API_URL}/api/session/{sid}", auth=AUTH)
                        _c, _t = METER.extract_and_record(bident, sisi.get("data") or {})
                        log(f"💰 meter: {_c:,.4f} USD, {_t} token hari ini total {METER.today(bident)}")
                    profiler["jalan"] = False
                    log(f"🧠 {persona['nama']}-jawab:", (jawab or "⚠️ GAGAL")[:80])
                    if not jawab:
                        return
                    if jawab.strip().upper().startswith("DIAM"):
                        log(f"🤫 {persona['nama']} memilih diam (otonom).")
                        return
                    if banyak > 1 and not cfg.get("otonomi"):
                        jawab = f"💬 *{persona['nama']}*\n{jawab}"
                    kirim(bot["token"], chat_id, jawab)
                except Exception as e:
                    log("❗ error proses:", repr(e))

            otonom = False  # dipanggil langsung → jawab normal; ruang inisiatif di initiatif_loop
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
        raise RuntimeError("owner_ids harus diisi sebelum jalan")
    with open(os.path.join(HERE, "bridge.pid"), "w") as handle:
        handle.write(str(os.getpid()))
    token_ok = [b for b in cfg["bots"]
                if not b.get("token", "").startswith("ISI") and len(b.get("token", "")) > 20]
    if not token_ok:
        print("⚠️  Belum ada token bot! Tempel ke config.json lalu jalankan lagi.")
        return
    migrasi_cron()  # cron.json → tabel schedules, sekali saat hidup
    pulih_chats_json()  # chats.json warisan → tabel chats
    sinkron_persona()   # personas.json ↔ tabel personas + cermin
    pulihkan_tugas_beku()
    threading.Thread(target=cron_loop, daemon=True).start()
    threading.Thread(target=initiatif_loop, daemon=True).start()
    threading.Thread(target=tugas_loop, daemon=True).start()
    threading.Thread(target=provision_loop, daemon=True).start()
    threading.Thread(target=outbox_loop, daemon=True).start()
    # 🧍 imunitas: thread mati → dihidupkan ulang otomatis
    threads = {}
    def nyalakan_ulang():
        for i, b in enumerate(token_ok):
            threads[i] = threading.Thread(target=jalankan_bot, args=(b, i), daemon=True)
            threads[i].start()
    nyalakan_ulang()
    log(f"🚀 bridge jalan, {len(token_ok)} token, personas: {len(muat_persona())}")
    while not SHUTDOWN.is_set():
        SHUTDOWN.wait(30)
        for i, b in enumerate(token_ok):
            if not threads.get(i) or not threads[i].is_alive():
                log(f"🔧 thread bot-{i} mati, nyalakan ulang...")
                threads[i] = threading.Thread(target=jalankan_bot, args=(b, i), daemon=True)
                threads[i].start()
    log("🛑 bridge selesai, semua thread dibiarkan secu tutup.")


if __name__ == "__main__":
    main()
