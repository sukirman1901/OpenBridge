# Changelog

All notable changes to OpenBridge are documented here. This project follows the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format and uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- Reply latency: status/typing/acknowledgement Telegram calls run off the
  execution thread in a bounded, coalescing presence service. A wire fence
  prevents stale cosmetic edits from overwriting a final answer, and an
  ambiguous acknowledgement send is never replayed on the network.
- Warm replies skip the queued text acknowledgement roundtrip by default;
  `/jobs` remains the diagnostic view and paused chats keep a truthful notice.
- Prepared sessions are no longer re-briefed after a restart (durable
  briefing memo, migration 5); failed briefings are never cached.
- Polling honors Telegram `retry_after` with capped exponential backoff, and
  transient identity-check connection failures retry instead of idling.
- Telegram HTTPS handling explicitly separates TLS setup from request writes:
  pre-request connection failures retry cheaply, while a written request body
  is never replayed.
- Outbound file delivery is isolated from media uploads (`media` delivery
  class, migration 5), so a slow file upload cannot hold a text reply.
- Deliverable paths are restricted to `hasil/` with a bounded no-symlink
  reader; multipart boundaries are unpredictable and filenames cannot inject
  headers into the upload envelope.
- The executor reconciler re-scans with a cursor so more than 32 active old
  receipts cannot starve newer completed receipts.
- Persist backend submission intent and accepted message IDs per job/run/purpose.
  Recover exact terminal results after restart or disconnect without submitting
  the prompt again. Missing receipts and unverified backend state remain fenced.
- Use reconnectable SSE hints with HTTP-history fallback, distinct bounded
  approval/execution budgets, and truthful waiting-approval/reconnecting statuses.
- Treat V2 compaction as asynchronous work with its own persisted receipt;
  `/new` no longer races a queued compaction against a new prompt.
- Add drain-before-restart and opt-in live recovery smoke commands under deploy/.
- Never quota-retry a prompt that already invoked tools; render file results
  safely in `/result` instead of raising a missing-text error.
- Ordinary tool-using chat requests have a 15-minute execution budget rather
  than timing out before the five-minute owner-approval window can finish.
- Verified late backend results can be settled by an operator and delivered
  durably without replaying execution.
- Aborted backend turns without idle history no longer falsely fence a chat
  when the backend is verified inactive with an empty inbox. Unknown states
  still require review; interrupted execution is never automatically replayed.
- Intake notices distinguish queued requests from interrupted-conversation
  blocks; only running requests display Processing.
- Plain-language chat requests can now hand off files through the same durable
  upload pipeline as background tasks. Capability guidance refreshes existing
  sessions each turn; slash commands are not required.
- Tomorrow/lusa scheduling phrases with a time are recognized as one-shot
  schedules, including "buatkan jadwal untuk saya besok jam 9".
- Ordinary Telegram replies show a simple Processing status instead of internal
  job numbers; `/jobs` remains available for diagnostics.
- Typing is established before model work and is not shown for unaddressed
  group messages. Regression tests synchronize with events rather than thread timing.
- Historical audit correction: typing and SSE streaming existed before the
  modular refactor. Current typing has been restored; SSE streaming has not.

## [0.3.0] - 2026-10-31 — Routine engine

### Added
- **Per-item routine management in natural language**: `jeda jadwal 2` pauses,
  `lanjutkan jadwal 2` resumes, `ubah jadwal 2 ke jam 9` edits, and
  `batalkan jadwal 2` deletes a single numbered routine. The bare phrase
  "batalkan jadwal" no longer deletes everything — it now asks which routine,
  and wiping a chat's schedules requires the explicit "batalkan semua jadwal".
- **Weekly routines from plain language**: `tiap senin jam 8` and
  `every friday 09:15` persist a real cron expression (`00 08 * * 1`), so a
  stated weekday keeps its weekday.
- **/jadwal panel with ids and states**: rows show `#id`, kind or `⏸
  paused`, human-readable expressions, and deletion buttons with the id.
- New durable store operations: `get_schedule`, `set_schedule_active`
  (pause/resume without deletion), and `update_schedule_expr` (edit time
  while preserving kind/prompt). No schema change; the migrations stay 1/2/3.

### Changed
- Routine parsing moved into `openbridge/services/routines.py` as pure,
  tested functions (`parse_directive`, `parse_creation`, `format_expr`);
  `Schedules.interpret` is now a thin dispatcher that applies account and chat
  ownership guards before any item op.
- 25 new tests (`test/test_routines.py`); full suite is now 366 passing.

## [0.4.0] - 2026-10-31 — Autonomy hardening (memory, files, guards)

### Added
- **Cancel queued jobs**: `/cancel <id>` in the owner's private chat (and a
  Cancel button on `/job <id>`) cancels durable work that has not started
  running — including quota-deferred jobs. Running jobs still can't be
  cancelled here; the claim fence keeps an already-submitted worker safe.
- **Long-term memory (SQLite)**: owners can teach durable facts with
  `/remember <fact>`, list them (`/memory`), and retire one (`/forget <id>`;
  deactivating keeps the audit trail). Active chat-specific facts (with global
  ones after them) are injected into every conversation briefing as a
  PERSISTENT MEMORY section the agent must never contradict.
- **Outbound files**: background agents can hand produced files to the chat —
  a final message with a line `@file hasil/x.ext` (or `@photo` for images)
  becomes a durable `document`/`photo` delivery uploaded via Telegram
  sendDocument/sendPhoto. Paths are resolved strictly inside the bridge home
  and must exist, so an agent cannot exfiltrate anything else.
- **Untrusted-content markers**: user messages and attachments reach the agent
  wrapped in `⟦UNTRUSTED ... ⟧ ... ⟦/UNTRUSTED⟧` markers with an explicit
  source and a data-not-directives note; embedded marker tokens inside user
  text are neutralized so injection cannot fake the closing marker. The
  briefing states the rule once, globally.
- **MCP connector template**: `deploy/mcp.example.jsonc` documents mergeable
  `mcp` entries for Gmail, Google Drive, and GitHub with `{file:*}` credential
  indirection and per-server setup notes; a unit test keeps real secrets out.
- **Typing indicator**: while the agent prepares its answer, the bot sends
  `sendChatAction` (`typing…`) for the duration of the work, best-effort and
  non-blocking — pacing the presence loop never delays or breaks a job.

### Fixed
- **One-shot anchors are timezone-proof**: stored one-shot expressions are
  UTC-naive, but the firing evaluation read them as the host's local time —
  on a `TZ=Asia/Jakarta` host every reminder would have fired seven hours
  early. The once-kind occurrence now reads stored expressions as UTC
  explicitly, and CI runs the whole suite again under `TZ=Asia/Jakarta`.
- **Provider-quota pauses a job instead of failing it**: when the backend's
  idle outcome reports a limit (e.g. `provider.chatgpt-limit`), the prompt
  never executed (zero tokens), so the job is now deferred (`RetryLater`,
  15-minute wait) and resumes at the next pump; ordinary provider failures
  still end the job durably. Live-verified trigger: a one-shot reminder fired
  9.7 s after its stored target (AUDIT.md).

## [0.2.0] - 2026-10-31 — Modular autonomous-agent runtime

### Added
- **Settled-job retention**: terminal jobs (succeeded/failed/cancelled) older than
  `retention.days` (default 30) whose deliveries are all final are pruned together
  with their durable rows, every 6 hours. Interrupted, queued, and running jobs —
  and pending/sending/uncertain deliveries — are never pruned.
- **Priority reminder lanes**: schedule-kind reminders pass the same-lane
  conversation leader (never the interrupted fence), dispatch before background
  backlog through a dedicated in-memory key, and deliver before pending task
  output backlogs.
- **`supervise.sh`**: supervisor loop with `Restart=on-failure` semantics and
  exponential backoff for hosts without an init system; `deploy/` ships a systemd
  unit, a logrotate file, and `config.example.json`.
- **Verification-first release cycle**: every landed stage deploys under
  supervision and is verified live (Telegram `getMe`/commands, OpenCode
  `/api/info`, SQLite integrity and `foreign_key_check`, preserved job counts),
  with results recorded in `AUDIT.md`.

### Changed
- `bridge.py` is now a seven-line, import-safe entry point. Importing it, the
  bootstrap, or any package module performs no configuration, database, network,
  or thread startup. `openbridge.bootstrap.create_application()` is the explicit
  per-instance composition root; `Application.run()` is the only startup path.
- Responsibilities are extracted into layered modules: `domain/`, `ports/`,
  `runtime/`, `services/`, and `adapters/` (Telegram ingress/commands/media/
  approvals/delivery, OpenCode execution, SQLite durable persistence).
- Tests build explicit isolated component graphs instead of patching legacy
  bridge globals; monolith-era tests are retained as regression coverage under
  `test/legacy/` until their uniquely-covered cases are ported.
- Legacy `store.py`, `task_queue.py`, `context_buffer.py`, `usage_meter.py`,
  `rate_limiter.py`, `approval_relay.py`, `runtime_policy.py`, and
  `scheduler.py` stay as root compatibility libraries instantiated by the
  factory — persisted names, files, and schema (migrations 1/2/3) unchanged.

### Removed
- **SSE/token-streaming delivery** and the Stop button, ▶️ Continue chunks, and
  streaming progress pings that rode on it: replies now persist a terminal
  result with delivery intents and deliver once, so retries never re-run the
  model. Owner Stop control remains via the scoped `st-` callback on live jobs.

## [0.1.0] - 2026-10-30

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
