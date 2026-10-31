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

## Run

```bash
# 1. Create a bot with BotFather. Save its token privately in bot-1.token.
#    Use mode 0600. Do not put credentials in shell history or chat.

# 2. Configure config.json with a bots array and explicit owner_ids.
#    Each bot: "token": "{file:bot-1.token}", "nama": "Una", "persona": "..."
#    Also include "trigger": {} for a minimal setup.

# 3. Start the OpenCode service, then (one-time per container boot):
./supervise.sh &   # detached supervision; keeps the bridge alive across crashes
# Later, ordinary operator commands:
./stop.sh          # scoped SIGTERM shutdown; supervision stops with it
```

`start.sh` alone is a foreground process protected by a single-poller lock.
`supervise.sh` adds automatic recovery on hosts without an init system
(Termux/proot containers): abnormal exit restarts with exponential backoff
(5 s → 5 min cap, reset after 30 min of stability); a clean exit ends
supervision, so a deliberate `./stop.sh` is never fought over. `deploy/`
contains a systemd unit for hosts that do run systemd; this environment does
not. `OPENBRIDGE_HOME` optionally selects configuration/state;
`OPENBRIDGE_SERVICE_JSON` optionally selects the OpenCode service registration.

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
