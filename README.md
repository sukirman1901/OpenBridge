# OpenBridge 🌉

[![test](https://github.com/sukirman1901/OpenBridge/actions/workflows/test.yml/badge.svg)](https://github.com/sukirman1901/OpenBridge/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An autonomous agent runtime powered by OpenCode,
with Telegram as the first communication adapter (WhatsApp and other channels
are future integrations).
Python standard library only, with SQLite state.

See [ARCHITECTURE.md](ARCHITECTURE.md) for implemented boundaries, limitations,
and the staged migration plan. New code and documentation use English;
existing configuration and data remain compatible.

## Features

| Module | Responsibility |
|---|---|
| `openbridge/domain/`, `openbridge/ports/` | Neutral identities and integration contracts |
| `openbridge/config.py` | Explicit configuration/path loading and worker validation |
| `openbridge/app.py`, `openbridge/bootstrap.py` | Lifecycle and explicit, per-instance composition factory |
| `openbridge/runtime/` | Persistent jobs and reserved chat/background worker pools |
| `openbridge/services/` | Durable schedule/task/initiative producers and execution |
| `openbridge/services/{delivery,sessions,job_commands}.py` | Output capture, session ownership, scoped owner job controls |
| `openbridge/adapters/` | Telegram, OpenCode, HTTP, and versioned SQLite runtime storage |
| `bridge.py` | Seven-line, import-safe command-line entry point |
| `store.py`, `task_queue.py` | SQLite state, delivery retries, autonomous tasks |
| `scheduler.py` | Cron and one-shot scheduling in UTC+7 |
| `rate_limiter.py`, `usage_meter.py` | Per-account request and daily budget limits |
| `context_buffer.py` | Conversation context buffer |
| `approval_relay.py`, `runtime_policy.py` | Owner approvals and access checks |

## Quick start (install in one line)

```bash
curl -fsSL https://raw.githubusercontent.com/sukirman1901/OpenBridge/main/install.sh | sh
```

This downloads the project into `~/OpenBridge` and runs the interactive
wizard, which asks three questions and writes a safe `config.json`:

1. **Bot name** — how your bot is displayed in Telegram (e.g. `Una`).
2. **Bot token** — create a bot first with [@BotFather](https://t.me/BotFather)
   (`/newbot`, then copy the token that looks like `123456789:AAAA…`).
3. **Your Telegram user id** — message [@userinfobot](https://t.me/userinfobot)
   to get it. This id becomes the **owner**: only you get job controls,
   approvals, and management commands.

The wizard saves the token to a private `<name>.token` file (mode 0600) and
references it from `config.json`, so credentials never sit in the config
itself.

Prefer manual setup? Clone the repo, then run `python3 wizard.py` yourself, or
copy [`deploy/config.example.json`](deploy/config.example.json) to
`config.json` and fill it in:

```json
{
  "bots": [
    {
      "nama": "Una",
      "persona": "You are Una, a helpful daily assistant.",
      "token": "{file:una.token}"
    }
  ],
  "owner_ids": [123456789],
  "trigger": {},
  "runtime": { "interactive_workers": 1, "background_workers": 1 },
  "retention": { "days": 30 }
}
```

Beyond the wizard's minimum you can tune: `budget.max_call_per_hari` /
`max_cost_hari` (daily OpenCode budget), `rate_limiter.max_per_menit` (per-chat
throttle), `trigger.cooldown_detik` / `max_char_reply` (reply etiquette), and
`retention.days` (1–365; terminal jobs older than this are pruned).

## Prerequisites

- **Python ≥ 3.10** — standard library only, nothing to `pip install`.
- **[OpenCode](https://opencode.ai) installed and running** — the bridge talks
  to a local `opencode serve` (discovered via
  `~/.local/state/opencode/service.json`). Set `OPENBRIDGE_SERVICE_JSON` to
  point at a server on another host/port.
- **A Telegram bot token** from @BotFather.

## Start the bridge

```bash
./supervise.sh &   # detached supervision (recommended)
```

or `./start.sh` for a foreground run protected by a single-poller lock.
`supervise.sh` adds automatic recovery on hosts without an init system
(Termux/proot containers): abnormal exit restarts with exponential backoff
(5 s → 5 min cap, reset after 30 min of stability); a clean exit ends
supervision, so a deliberate `./stop.sh` is never fought over. Later operator
commands:

```bash
./stop.sh          # scoped SIGTERM shutdown; supervision stops with it
```

`deploy/` contains a systemd unit for hosts that do run systemd.
`OPENBRIDGE_HOME` optionally selects configuration/state.

## Use the bot

Open a private Telegram chat with your bot and just talk to it — it replies
through the OpenCode agent. In group chats it stays **silent by default** and
only answers when addressed or replied to. The owner gets extra powers in
their private chat with the bot:

| Command | What it does |
|---|---|
| `/help` | 📖 Command menu |
| `/model` / `/agent` | 🧠 Swap the OpenCode model / agent |
| `/persona` | 👥 View/manage bot personas |
| `/jadwal` | ⏰ Automatic schedules (cron + one-shot reminders, UTC+7) |
| `/tugas <goal>` | 📋 Hand off and track an autonomous task |
| `/status` | 📊 Bot status (uptime, budget, queues) |
| `/jobs`, `/job <id>` | 📋 Durable job and delivery status |
| `/result <id>` | 📄 Retrieve a saved job result without re-executing |
| `/resolve <id>` | 🔎 Reconcile an interrupted job after a crash |
| `/new`, `/reset` | ♻️ Fresh context / wipe memory |

**Routines in plain language** — create them by chatting, and manage each one
by its id from `/jadwal`:

- Create: `remind me tomorrow at 5` · `ingetin aku besok jam 5` ·
  `jadwalkan konten jam 8` · `tiap senin jam 8` (real weekly cron)
- Manage: `jeda jadwal 2` (pause) · `lanjutkan jadwal 2` (resume) ·
  `ubah jadwal 2 ke jam 9` (edit) · `batalkan jadwal 2` (delete one) ·
  `batalkan semua jadwal` (delete all — explicit on purpose)

Everything a job does persists in SQLite first: send survives restarts,
ambiguous sends are quarantined instead of duplicated, and nothing replays
silently after a crash — you decide via `/resolve`.

## Security and limitations

- Store tokens in private files, not inline in configuration or source code.
- Configure explicit owner IDs and keep OpenCode tool permissions enabled.
- Owner checks and approval prompts are not an OS sandbox.
- Databases, logs, memories, and backups may contain sensitive data; keep them private.
- Incoming authorized Telegram messages are committed to SQLite before the cursor advances.
- Queued work survives restarts; interrupted execution is fenced for owner review, never blindly replayed.
- Reminders (schedule jobs) are priority work: they dispatch before other
  background backlog in the same chat, never wait behind a long-running task's
  in-memory queue, and their messages deliver before other background backlog —
  yet the interrupted fence still blocks them until you reconcile.
- Model results and delivery intents are saved together. Delivery retries do not re-run the model.
- Ambiguous sends are quarantined to avoid duplicate messages; use `/result <id>` to retrieve saved output.
- Settled-job retention: terminal jobs older than `retention.days` (default 30,
  range 1–365) whose deliveries are all final (sent/dead) are pruned together
  with their durable rows, every 6 hours. Interrupted, queued, running jobs and
  unresolved deliveries (`pending`/`sending`/`uncertain`) are never pruned.
- WhatsApp and voice transcription are not implemented yet.

## Verify

```
python3 -m unittest discover -p 'test_*.py'
python3 -m compileall -q bridge.py openbridge
```

## Roadmap

- [x] Legacy SQLite state, scheduling, autonomous tasks, and owner approval relay.
- [x] Runtime foundation: transport adapters and bounded conversation dispatch.
- [x] Durable Telegram inbox, jobs/runs, saved results, outbox, and conservative restart recovery.
- [x] Move scheduler, autonomous tasks, and initiative onto the durable runtime.
- [x] Reserve separate chat/background execution lanes; preserve FIFO within each lane.
- [x] Extract configuration, lifecycle, polling, delivery, session mechanics, and owner job controls.
- [x] Extract regular commands/callbacks, persona/provisioning, media, maintenance, and job routing.
- [x] Explicit application bootstrap; no database/network/thread startup during imports.
- [ ] Extract remaining channel-specific services and define fairness/retention policies.
- [ ] Complete channel-neutral orchestration and persistence.
- [ ] WhatsApp gateway adapter.
- [ ] Process supervision and OS-level execution isolation.

## Job controls (owner's private chat)

- `/jobs`: recent execution and delivery status.
- `/job`: alias for `/jobs`; `/job <id>` shows details, task progress, and Stop
  for a running job with a checkpointed session.
- `/result <id>`: retrieve persisted output without re-executing the job.
- `/resolve <id>`: show reconciliation instructions for an interrupted job.
- `/resolve <id> confirm`: after inspecting its actions in OpenCode, cancel the
  interrupted attempt and release queued jobs. A checkpointed session must be
  inactive with an empty inbox. This never replays the interrupted attempt.

Interactive jobs now use a persistent status message, updated from received to
processing to the saved final reply. Token-by-token streaming remains a legacy
path, not the durable result-delivery path. Startup performs an additive,
versioned migration of `groupbot.db`; it does not rename or drop legacy tables.

Background work uses an independent session per job and the same persistent
execution/delivery pipeline. Safe quota pauses resume from completed steps;
uncertain executions still require owner review. Long tasks no longer hold the
ordinary chat queue: chat and background workers have separate capacity and
sessions. Job controls and approvals remain available outside those queues.
`/tugas <goal>` reports both its task ID and durable job ID. Exhausting its
step cap without DONE fails the task instead of falsely reporting completion.

Default execution capacity is one interactive worker plus one background worker.
Configure `runtime.interactive_workers` and `runtime.background_workers` to
change it. The old `runtime.conversation_workers` remains an interactive-only
fallback. API quotas, Telegram delivery, and workspace access are still shared;
this is not a latency guarantee or filesystem sandbox. Reminders still share
the background queue with tasks. Interrupted work fences both lanes for review.

## Refactor boundary

New modules import without reading user configuration, opening runtime databases,
starting threads, or importing `bridge.py`. Their dependencies are injected.
Delivery and job controls check channel/account/conversation scope; backend idle
and permission checks are implemented by the OpenCode adapter, not the controller.

`bridge.py` is now a thin entry point. `openbridge.bootstrap.create_application()`
loads configuration and builds one isolated component graph; it does not start
workers or contact providers until execution is explicitly requested. Only
`Application.run()` starts the runtime/pollers. The factory accepts injected
backend/channel clients for tests and closes partially constructed resources on
failure. `Application.close()` releases storage after workers/auxiliary threads
have stopped, never while they may still be using it.

```text
openbridge/
  app.py, bootstrap.py, config.py
  domain/, ports/, infrastructure/
  runtime/                  # Persistent jobs, lanes, bounded dispatch
  services/                 # Conversations, personas, provisioning, schedules,
                            # maintenance, sessions, output, router, job controls
  adapters/
    channels/telegram/      # Ingress, commands/callbacks, media, approvals, delivery
    execution/opencode/    # Native backend API
    persistence/sqlite/    # Durable inbox/jobs/runs/outbox
```

Legacy `store.py`, `task_queue.py`, `rate_limiter.py`, `usage_meter.py`,
`context_buffer.py`, and `approval_relay.py` remain compatibility libraries;
their instances are owned by the factory, not module globals. Existing persisted
field names and files are retained. Full cross-channel identity/memory migration,
OS/process supervision, and WhatsApp integration remain separate work.

## License

[MIT](LICENSE)
