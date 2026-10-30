# Changelog

All notable changes to OpenBridge are documented here. This project follows the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format and uses [Semantic Versioning](https://semver.org/).

## Unreleased

### Added
- **`OPENBRIDGE_SERVICE_JSON`** env var: point OpenBridge at an OpenCode server running on another host/port (default remains `~/.local/state/opencode/service.json`).
- Friendly startup error with step-by-step instructions when the OpenCode server is not running yet.
- README: full installation guide (install OpenCode → `opencode serve` → BotFather → `config.json` → `./start.sh`).

## [0.1.0] — 2026-10-30

First public release. 🎉

### Added
- Core Telegram ⇄ OpenCode bridge (`bridge.py`): long-polling courier, persistent LLM sessions per (persona, chat), owner-only commands and confirmation buttons.
- `[SILENT]`-by-default group etiquette with a periodic **initiative loop** — the model itself decides whether to speak or stay quiet in a group (legacy `DIAM` tokens still accepted).
- Autonomous **task queue** (`task_queue.py`): natural-language "do this for me" goals decomposed to `goal → plan → act (bounded steps) → verify → report`.
- **Scheduler v2** (`scheduler.py`): standard 5-field cron plus legacy `"21:00"` / `*/N` forms, evaluated in **Asia/Jakarta (UTC+7, WIB)** regardless of host timezone; one-shot reminders ("remind me tomorrow at 9pm") and repeating intervals.
- **SQLite store** (`store.py`): schedules, outbox (retry with exponential backoff), update-offset dedupe, per-bot usage, chats, personas — the single source of truth.
- **Rate limiter** (`rate_limiter.py`) with a sliding per-minute window, and **daily budget meter** (`usage_meter.py`) with hard call/cost caps per bot.
- **Runtime policy** (`runtime_policy.py`): owner-only authorization helpers and safe schedule ownership checks.
- Daily state backups (`backup/gb-YYYY-MM-DD.tar.gz`, last 7 kept) and persona-archival before deletions.
- 84 unit tests (`python3 -m unittest discover -p "test_*.py"`), a CI workflow, and MIT licensing.

### Security
- Bot tokens are never stored in `config.json` — the `{file:...}` indirection resolves them from per-bot token files excluded by `.gitignore`.
- Destructive actions (persona deletion, server-side provisioning) require an explicit owner confirmation button.
