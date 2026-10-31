# Changelog

All notable changes to OpenBridge are documented here. This project follows the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format and uses [Semantic Versioning](https://semver.org/).

## Unreleased

### Added
- **V2 approval relay**: owner-only private Telegram Approve once / Deny buttons for pending permission requests from registered bridge sessions. Requests expire after five minutes and are rejected while the relay is running; network failures never grant approval. No durable `always` approvals are offered. Shell and file edits now require approval; unknown/MCP actions default to ask, and subagents are blocked until child-session routing is supported.
- **Correction to the previous security pass**: native OpenCode V2 uses `permissions` arrays with `shell`/`subagent`. The previous switch to V1 `permission` maps was incorrect. Sessions now receive explicit V2 rules on creation/reuse; new sessions also specify the bridge location. Permission controls are not an OS sandbox.
- **`OPENBRIDGE_SERVICE_JSON`** env var: point OpenBridge at an OpenCode server running on another host/port (default remains `~/.local/state/opencode/service.json`).
- Friendly startup error with step-by-step instructions when the OpenCode server is not running yet.
- README: full installation guide (install OpenCode → `opencode serve` → BotFather → `config.json` → `./start.sh`).
- Tests moved into a `test/` folder; run them with `python3 -m unittest discover -s test -t .`.
- One-line installer: `curl -fsSL https://raw.githubusercontent.com/sukirman1901/OpenBridge/main/install.sh | sh` plus an interactive `wizard.py` (token file + config generation, OpenCode server check).
- **Telegram streaming** for chat replies: the bridge consumes OpenCode's SSE event bus (`/api/event`, `session.text.delta`) and streams the answer into Telegram by editing one growing message (typing indicator kept during generation); graceful fallback to plain polling when the SSE feed is absent.
- **Attachment intake**: photos, documents, videos, voice notes and audio sent to the bot are downloaded to `media/` (Bot API 20 MB cap) and offered to the agent as local files — text documents are read as-is, images work with vision-capable models, media-only types are described by metadata.
- **Rich text in Telegram** — chat replies send with Telegram formatting (`**bold**`, `_italic_`, `` `code` `` blocks); a malformed entity falls back to plain text instead of dying on HTTP 400. Streaming messages finalize with formatting too; interim stream edits stay plain (incomplete entities mid-stream would flicker).
- **Deep-work sessions**: `tugas`/`cron` sessions get a work-mode briefing (chat brevity rules lifted, tools + real verification required, deliverables saved under `hasil/`); new `tugas` config keys — `max_steps` (default 10) and `wait_detik` (default 240).
- **Stop streaming** — a ⏹ Stop button rides on every streamed message; owner clicks it and the bot finalizes with "stopped by owner" (tries the OpenCode `POST /session/:id/abort` endpoint first, falls back to a bridge-side stop flag that works on OpenCode v2.2 where the endpoint is absent).
- **"Continue" for clipped replies** — answers longer than `max_char_reply` are no longer silently truncated: a ▶️ Continue button delivers the remainder in 3500-char chunks, chained until done.
- **Task progress visibility** — claiming a task sends an acknowledgment ("🫡 Task #x claimed… budget N steps") and every 3rd step sends a progress ping ("⚙️ step x/y"), so autonomous work no longer feels like a black hole.
- **`/new` command** — fresh context without losing the thread: the old session is compacted server-side, an auto-generated handoff summary (max 120 words) is carried into the new session; `/reset` still wipes fully.
- **Auto-compact** — private chat sessions are compacted via OpenCode `POST /session/:id/compact` after a configurable number of turns (`compact_per_turns`, default 120) to keep the context window healthy over long running chats.
- **Media janitor** — attachments in `media/` older than 7 days are removed during the daily backup sweep.
- **Security hardening** — authorization runs before attachment downloads. Token files and `pool.json` are excluded from the backup file list; credential-bearing files are chmod 600 at startup. Backups can still contain sensitive configuration, memory, and database content and must be protected. Native V2 permission configuration is corrected in the approval-relay pass above.

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
