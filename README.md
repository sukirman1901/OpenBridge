# OpenBridge 🌉

**Telegram ⇄ OpenCode autonomous bot bridge** — a self-hosted Telegram bot that pairs with the [OpenCode](https://opencode.ai) API to run an autonomous agent: scheduled cron jobs, self-initiated conversation, a goal-driven task queue, and a silent-by-default etiquette in group chats. Pure Python standard library (no `pip install`), with SQLite as the single source of truth for state.

> Bot names in the configuration (e.g. `Unabot`) are just **sample personas** — this project is open source: clone it, rename the persona and point `config.json` at your own bot token, and make the bot yours. 🤖

## Features

| Module | What it does |
|---|---|
| `bridge.py` | Core Telegram ⇄ OpenCode courier: persistent sessions per (persona, chat), `[SILENT]` mode (legacy `DIAM` still accepted), initiative loop, task loop, daily backups |
| `store.py` | SQLite store (6 tables): schedules, outbox (retry + backoff), seen (offset dedup), usage, chats, personas |
| `scheduler.py` | Cron scheduler: standard 5-field expressions plus legacy `"21:00"` / `"*/N"`, constant **WIB / Asia-Jakarta (UTC+7)** evaluation from epoch |
| `task_queue.py` | Autonomous task queue: goal → plan → act → verify → report |
| `rate_limiter.py` | Per-bot sliding-window call-rate limiter (`max_per_menit` per minute) |
| `usage_meter.py` | Daily per-bot call & token/cost meter with hard budget caps |
| `context_buffer.py` | Per-conversation context buffer (ring of recent turns) |
| `runtime_policy.py` | Guardrails: owner-only policy and permission checks |

## Quick start

```bash
# 1. Create a bot with @BotFather → copy the token into a file
echo "123456789:AA..." > bot-1.token

# 2. Fill in config.json
#    "token": "{file:bot-1.token}", "nama": "YourBotName",
#    "owner_ids": [your-telegram-user-id]

# 3. Start the OpenCode server (opencode serve), then:
./start.sh    # launch the bridge (lock-protected)
./stop.sh     # stop it safely (SIGTERM)
```

The important `config.json` keys: `bots` (each with `nama`, `token`, `persona`), `owner_ids`, `trigger` (cooldown + initiative interval), `budget` (daily call/cost caps), `rate_limiter`, and `cron`. Tokens are **never inlined** — always use the `{file:...}` indirection. See the repository's own `config.json` for a complete working example.

## Security

- Bot tokens **must never be written in plain text into `config.json`** — always use the `{file:botname.token}` indirection; `*.token` files are excluded by `.gitignore`.
- A single owner is recognized via `owner_ids`; non-owner messages and callbacks are ignored (fail-closed), and destructive actions require owner confirmation in chat.
- Persona edits and deletions are recorded; daily state backups land in `backup/gb-YYYY-MM-DD.tar.gz` (7 kept).

## Structure

```
bridge.py, store.py, scheduler.py, task_queue.py, rate_limiter.py,
context_buffer.py, usage_meter.py, runtime_policy.py
config.json, start.sh, stop.sh
84 unit tests in `test_*.py` — run: python3 -m unittest discover -p "test_*.py"
GOAL.md (master plan, phases 0–5) · AUDIT.md (audit trail)
```

## Development

```bash
python3 -m unittest discover -p "test_*.py"
```

## Roadmap

- [x] Phase 1 — SQLite store + outbox retry + JSON → DB migration
- [x] Phase 2 — Scheduler cron v2 (WIB) + one-shot + interval triggers
- [x] Phase 3 — Autonomous task queue + initiative loop + rate limiter
- [ ] Phase 4 — Cloud host (VPS/box) for 24/7 isolation
- [ ] Phase 5 — Auto-provisioning of new bots (owner confirmation button)

## License

[MIT](LICENSE) — free to use, modify, and distribute. Go build your own bot! 🚀
