# OpenBridge 🌉

**Bridge Telegram ⇄ OpenCode API** — bot Telegram otonom dengan diskusi antar-bot, jadwal cron, tugas mandiri, dan mode `DIAM`. Standar library Python saja (tanpa pip install), SQLite sebagai sumber kebenaran state.

> Nama bot di konfigurasi ini (`Unabot`) hanyalah **contoh persona** — proyek ini open source: clone, ganti nama persona & token bot milikmu di `config.json`, jadilah bot-mu sendiri. 🤖

## Fitur

| Modul | Fungsi |
|---|---|
| `bridge.py` | Kurir Telegram ⇄ OpenCode: sesi persisten per (persona, chat), mode `[DIAM]`, inisiatif, tugas, backup harian |
| `store.py` | SQLite (6 tabel): schedules, outbox (retry + backoff), seen (dedupe offset), usage, chats, personas |
| `scheduler.py` | Cron 5-field, zona WIB (UTC+7) dari epoch, kind `once`/`interval`/legacy |
| `task_queue.py` | Antrean tugas otonom: goal → plan → act → verify → report |
| `rate_limiter.py` | Pembatas `max_per_menit` per bot |
| `usage_meter.py` | Meteran panggilan & token per hari per bot |
| `context_buffer.py` | Buffer konteks percakapan |
| `runtime_policy.py` | Guardrail owner-only & izin |

## Cara pakai (ringkas)

```bash
# 1. Buat bot di @BotFather → salin token ke file
echo "123456789:AA..." > bot-1.token

# 2. Isi config.json
#    "token": "{file:bot-1.token}", "nama": "NamaBotMu",
#    "owner_ids": [id-telegram-kamu]

# 3. Jalankan OpenCode server (opencode serve) lalu:
./start.sh    # mulai bridge (nohup + pid + lock)
./stop.sh     # hentikan dengan aman (SIGTERM)
```

Isi `config.json` minimal: `bot_ids`, `nama`, `token` (`{file:...}` — token tidak pernah inline), `owner_ids`, `reply_chance` (DIHAPUS — inisiatif via `initiatif_loop`), dll. Contoh lengkap ada di `config.json` repo ini.

## Keamanan

- Token bot **tidak boleh ditulis di config.json** — selalu pakai indireksi `{file:nama.token}`; `*.token` difilter `.gitignore`.
- Owner tunggal dikenali dari `owner_ids`; aksi berbahaya butuh konfirmasi owner di chat.
- Edit persona/hapus data tercatat; backup harian di `backup/gb-YYYY-MM-DD.tar.gz` (keep 7).

## Struktur

```
bridge.py, store.py, scheduler.py, task_queue.py, rate_limiter.py,
context_buffer.py, usage_meter.py, runtime_policy.py
config.json, start.sh, stop.sh
test_*.py (79 unit test) — jalankan: python3 -m unittest discover -p "test_*.py"
GOAL.md (rencana master fase 0–5) · AUDIT.md (jejak audit)
```

## Roadmap

- [x] Fase 1 — SQLite + outbox retry + migrasi JSON → DB
- [x] Fase 2 — Scheduler cron v2 (WIB) + one-shot + interval
- [x] Fase 3 — Task queue otonom + initiatif DIAM + rate limiter
- [ ] Fase 4 — Host cloud (VPS/box) untuk isolasi 24/7
- [ ] Fase 5 — Provisioning auto-spawn bot baru (button konfirmasi owner)

## Lisensi

[MIT](LICENSE) — bebas dipakai, diubah, didistribusikan. Selamat membuat bot-mu sendiri! 🚀
