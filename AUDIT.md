# Bridge audit

## Follow-up: checkpoint status leak (2026-10-31)

Owner's job #17 reproduced a missed path: bootstrap.checkpoint still edited
the status message with a job number after the router had simplified it.
Job #17 itself succeeded and its final edit was recorded sent. Changed the
checkpoint text to Processing and added a fully assembled ingress/execution
regression test. The test failed on the old checkpoint string before the fix.

## Telegram UX re-audit and historical corrections (2026-10-31)

- The earlier statement that typing never existed was incorrect. `git log -S
  sendChatAction` identifies the original implementation in `9a5c4be`, SSE
  streaming with typing in `bd611d5`, and its removal during modularization in
  `08db20c`. `3240c25` restored typing, not streaming. The v0.4.0 tag precedes
  that typing commit; release notes mentioning typing do not reflect the tag.
- Normal acknowledgement, processing, completion, and execution-error notices
  no longer expose job numbers. Explicit `/jobs` controls keep their identifiers.
- Unaddressed group messages no longer start typing. The initial typing request
  is made before answering, removing the scheduling race; tests use an Event
  instead of assuming the worker thread ran immediately.
- Both the default and `TZ=Asia/Jakarta` suites pass: 415 tests each. Restarted
  via stop.sh/supervise.sh; one supervisor and bridge process observed. SQLite
  quick_check=ok, foreign_key_check empty, 14 succeeded jobs preserved.
- Remaining limitations: current model execution polls for terminal results;
  SSE/token streaming is NOT restored. Typing ends at execution, not confirmed
  delivery. Marker framing is guidance, not an OS sandbox or injection-proof
  boundary. Existing outbound file validation allows the entire bridge home,
  including potentially sensitive files; earlier claims of complete
  exfiltration prevention were overstated. MCP examples have structural tests,
  not verified live OAuth connections. These require separate hardening and
  end-to-end validation; this audit does not declare them complete.

## Deployment of explicit bootstrap (2026-10-31)
- Full suite before deployment: 323 unit tests OK; `python3 -m compileall -q`
  over `bridge.py` and `openbridge/` clean.
- Deployed via `stop.sh`, lock acquisition, and `start.sh` (durable Python run).
- Post-restart health: Telegram `getMe` confirms `@Openuna_bot`; all 13 commands
  registered through the new poller; OpenCode `/api/info` reachable on 2.2.4;
  SQLite `quick_check=ok`; migration versions unchanged at [(1,), (2,), (3,)];
  no running jobs; job states interactive/succeeded ×7; legacy counts preserved
  (schedules 0, tasks 2, seen 1, outbox 1, usage 0, chats 1, personas 1).
- New implementation is live. Remaining future work unchanged: cross-channel
  persistence, WhatsApp adapter, retention, priority reminder lanes, OS
  isolation (supervision itself was added separately, see the next entry).

## Priority reminder lanes (2026-10-31)
- Reminder semantics without a schema change (migration versions stay 3):
  schedule-kind jobs may pass the same-lane conversation leader in both
  `pending()` and `claim()` — the interrupted fence still hides reminders until
  an owner reconciles; other kinds keep full conversation leadership.
- `pending()` orders earliest reminders before the background backlog window,
  so a large background batch cannot push a reminder past the SQL LIMIT.
- `_pump_lane` submits reminders under a dedicated in-memory dispatcher key
  (`"reminder"`), meaning long tasks in the same chat never serialize behind
  reminders or vice versa; a reminder only waits for an already-running
  reminder of that chat.
- `claim_delivery()` now orders interactive first, then schedule-kind, then
  other background deliveries, so reminder messages jump a pending task-output
  backlog.
- Eight tests: ordering, leadership bypass, non-schedule leadership unchanged,
  fencing intact, self-serialization, delivery priority, interactive acks
  precedence, and end-to-end concurrency (a reminder completes while a task
  holds its worker on a gate). Full suite: **341 tests OK**; `compileall` clean.
- Deployed under `supervise.sh`; health same as previous deployments
  (`@Openuna_bot`, `quick_check=ok`, `foreign_key_check` empty, migration
  versions [(1,), (2,), (3,)], 7 succeeded jobs preserved).

## Settled-job retention (2026-10-31)
- Added `JobStore.prune_terminal`: one transaction per job deletes a terminal
  job (succeeded/failed/cancelled) older than the window only when every
  delivery is final (sent/dead), removing its steps, runs, deliveries, trigger,
  and inbox rows. Interrupted/queued/running jobs and pending/sending/uncertain
  deliveries are never pruned; ingress cursors are never pruned. Reconciliation
  via `/resolve` refreshes `updated_at`, so a mistaken cancel ages out normally.
- Configure with `"retention": {"days": N}` (default 30, accepted 1–365;
  bools/strings/fractions rejected before any state opens). The bootstrap runs
  the prune every 6 hours and logs the count; `config.py` validates that
  `retention` is an object.
- Ten new tests cover: full-row pruning with hierarchy, failed/cancelled via
  real acceptance+finish or acceptance+claim+`/resolve` paths, age reset after
  reconciliation, batch limit oldest-first, unfinished states and ambiguous
  deliveries retained, dead deliveries prunable, and configuration boundaries.
  Full suite: **333 tests OK**; `compileall` clean.
- Deployed under `supervise.sh`; post-restart health identical to the previous
  deployment (Telegram `@Openuna_bot` confirmed, SQLite `quick_check=ok`,
  migrations [(1,), (2,), (3,)], 7 succeeded interactive jobs intact — all
  younger than the window, so nothing was pruned today).

## Process supervision deployment (2026-10-31)
- This host is Ubuntu proot under Termux/Android: no systemd as PID 1 and no
  cron, so `systemctl` cannot operate. `deploy/openbridge.service` and
  `deploy/logrotate` are kept for hosts that do run systemd/logrotate; the
  logrotate file is also installed at `/etc/logrotate.d/openbridge` but stays
  dormant until a scheduler runs it.
- Added `supervise.sh`: a detached supervision loop with systemd
  "Restart=on-failure" semantics — abnormal exit restarts with exponential
  backoff (5 s doubling, 5 min cap, reset after 30 min of stability); a clean
  exit ends supervision so a deliberate `./stop.sh` is never fought over.
  The single-poller lock prevents supervised and manual launches from running
  two pollers on one token (verified live: a manual run during supervision
  fails fast instead of double-polling).
- Live verification: SIGKILL of the bridge (exit 137) was detected and the
  bridge auto-recovered, coming back online with the persisted update offset;
  a deliberate `./stop.sh` drained cleanly (~53 s) and correctly ended
  supervision instead of restarting. Final state: supervised bridge online,
  `getMe` confirms @Openuna_bot with 13 registered commands.
- Boot persistence inside the Termux/proot container remains manual: after a
  device or container restart, run `./supervise.sh &` once (Termux:Boot is a
  possible follow-up outside this container). Note the OpenCode backend process
  is managed by the harness environment and is not supervised here.

## Explicit bootstrap and thin entry point (2026-10-31)
- Replaced eager root globals with a seven-line entry point and explicit
  bootstrap.create_application(). Configuration, SQLite, caches, runtime, and
  channel/backend clients are per-instance; imports create no runtime state.
- Extracted ordinary commands/callbacks, Telegram ingress/media/approval UI,
  conversations, personas, provisioning, schedules, maintenance, and job routing.
  This is responsibility-based composition, not a renamed monolithic bridge.
- Existing storage/limiter/context/approval modules remain compatibility libraries.
  Factory partial failures close opened connections; shutdown closes resources
  only once all workers/auxiliary threads have stopped.
- Tests now construct isolated real component graphs instead of patching legacy
  bridge globals. Old behavioral/security assertions are retained at service boundaries.
- Retired unused legacy SSE/token-streaming paths; durable terminal-result delivery
  remains the only normal response path. Fresh-context lookup/title and failed
  reset ownership handling corrected while extracting the conversation controller.
- Hardened account-scoped menus/Stop, owner-bound once-only provisioning, atomic
  config/mirror/backup updates, private media paths, and unknown inbox retention.
- No database migration, account credential rotation, new channel, OS supervisor,
  or sandbox added. Legacy persisted schema and script/state paths remain intact.
- Live isolated factory diagnostic against OpenCode passed: saved Telegram update
  produced a terminal FACTORY_OK result and durable delivery intent. No Telegram
  message was sent; its disposable, tool-denied session was cleaned up.

## Module-boundary extraction (2026-10-31)
- Extracted explicit configuration loading, validated worker settings, private
  credential repr handling, and sanitized backend registration errors.
- Added an injected Application lifecycle with signal restoration, shared worker
  drain deadline, context cleanup, and one per-account poller registry shared by
  provisioning and supervision. No process supervisor is introduced.
- Extracted Telegram polling and output formatting/classification, generic
  job-output capture, conversation/background sessions, compatibility briefing,
  and scoped owner job controls. New modules never import the legacy bridge.
- Output services and controllers check channel/account/conversation scope.
  Session reuse/idle/policy operations now use the execution adapter contract.
- Session-list lookup failures cannot silently create duplicates. Telegram plain
  fallback requires a confirmed HTTP 400 parse rejection, avoiding ambiguous send replay.
- Preserved launch paths, existing token-file/configuration formats, session
  titles, queues, and delivery semantics. No database migration in this pass.
- Remaining: eager root bootstrap, regular command/callback panels, persona and
  provisioning state, maintenance, and legacy streaming. bridge.py is not yet
  claimed to be a fully thin entry point or WhatsApp-ready implementation.
- Verification: 296 tests and compileall pass, including clean import boundaries,
  output routing, backend idle failures, signal restoration, and duplicate-poller
  prevention. Scoped restart succeeds with the new Application and poller.
- Real OpenCode /api/info and Telegram getMe succeed; poller reports online.
  SQLite quick_check passes, migration versions stay 1/2/3, all legacy table
  counts and seven completed job states are preserved. No diagnostic chat sent.

## Reserved chat/background execution lanes (2026-10-31)
- Source and consistent private database backups saved before the change.
- Migration 3 assigns persisted lanes by job kind without replaying or changing
  existing states. Database claim/pending checks enforce FIFO per lane while
  interrupted work continues to fence the entire conversation.
- Separate bounded worker pools reserve chat capacity independently of long
  background tasks. Independent query windows prevent backlog-limit starvation.
- Ready interactive deliveries get priority; result chunk order and ambiguous
  send quarantine remain unchanged. In-flight network requests are not preempted.
- Shutdown stops new claims and drains both pools under one shared deadline.
- Deterministic integration test holds a background task running while the same
  conversation's chat job completes in the reserved pool, with separate sessions.
- Limits: API quotas and filesystem access remain shared; no tool/file locking,
  deadline-aware reminder lane, weighted delivery fairness, or OS sandbox added.
- Verification: 264 tests and compileall pass. Scoped restart applied migration
  3; SQLite quick_check passes. All legacy table counts and the three completed
  interactive job states were preserved; real Telegram getMe succeeds.
- No live concurrent model/Telegram conversation was generated for this change;
  reserved-capacity behavior is verified by deterministic blocked-worker tests.

## Unified background runtime (2026-10-31)
- Added migration 2 for typed triggers, safe deferral timestamps, durable
  initiative generations, and completed task-step checkpoints.
- Atomic schedule acceptance replaces mark-before-thread execution. Task
  linking is idempotent and does not prematurely claim work. Internal trigger
  IDs never advance Telegram source cursors.
- Extracted background producers/execution into openbridge/services. Removed
  direct cron/task execution and memory-only initiative dispatch.
- Background calls use independent job sessions, native permissions, usage
  limits, terminal completion correlation, and the existing durable outbox.
- Safe quota pauses create deferred runs and resume from completed steps;
  interrupted/ambiguous calls remain fenced rather than automatically retried.
- Task projections and runtime completion commit together. Step exhaustion no
  longer falsely reports success without a DONE report.
- Added /job alias, per-job progress/Stop, and non-final step retrieval. /tugas
  now reports its durable job ID and honors configured step caps.
- Corrected the host-local cron fallback and duplicate entries in one import.
  Legacy once/interval timestamp interpretation remains unchanged.
- Limits: per-conversation head-of-line blocking remains; permissions are not
  OS isolation; WhatsApp, distributed workers, and voice remain unimplemented.
- Real OpenCode background diagnostic passed with tools denied: one step
  checkpointed, task done, output persisted. No Telegram diagnostic was sent;
  the disposable session was cleaned up.
- Verification: 253 tests and compileall pass. Scoped restart deployed migration
  versions 1 and 2; SQLite quick_check passes. All seven legacy table counts and
  both completed task statuses were preserved. Telegram getMyCommands confirms
  that both /job and /jobs are registered.

## Durable interactive runtime (2026-10-31)
- Source and consistent SQLite backups saved privately before migration work.
- Added transactional runtime inbox/cursors/jobs/runs/deliveries through a
  versioned additive migration. No legacy tables or configuration are rewritten.
- Telegram acceptance commits before advancing the cursor or acknowledging.
  Unauthorized events do not create jobs or retain their message bodies.
- Interactive output is captured, then job result and delivery intent commit
  together. Final delivery edits the persisted status message where possible.
- Queued jobs survive restarts. Running/ambiguous attempts become interrupted
  and fence their conversation until explicit owner reconciliation, not replay.
- /jobs, /result, and /resolve bypass blocked execution queues, with scoped
  account/conversation checks. Reconciliation checks known backend activity/inbox.
- OpenCode completion follows the accepted message ID through a terminal idle;
  newest-first histories are sorted. Partial replies are not considered success.
- Delivery retries do not execute the model. Ambiguous sends are quarantined;
  known-message edits may retry; rate-limit retries honor retry_after.
- Corrected Stop to use the native V2 interrupt route.
- Real backend contract smoke passed using a disposable, tool-denied diagnostic
  session; terminal text matched OPENBRIDGE_OK, then the session was cleaned up.
- Limits: single-poller recovery, no distributed leases, no automatic execution
  retry, no scheduler/task migration yet, no WhatsApp or voice transcription.
- Verification: 230 discovered tests and compileall pass. A real Telegram
  acknowledgement was edited into a persisted diagnostic result using an
  isolated temporary database; delivery was confirmed sent, without model replay.
- Deployed after a scoped stop/restart. Runtime migration version 1 applied;
  SQLite quick_check passed and all seven legacy table row counts were unchanged.
- First real incoming production job completed as succeeded; its saved result
  delivery was confirmed sent on the first attempt after deployment.
- Existing scheduler utcnow deprecation and legacy-test connection cleanup
  warnings remain; they are not test failures.

## Runtime foundation refactor (2026-10-30)
- Architecture direction and incremental boundaries documented in ARCHITECTURE.md.
- Added channel-neutral identities, integration ports, Telegram/OpenCode clients,
  and bounded FIFO dispatch keyed by channel/account/conversation.
- Direct turns, commands, ordinary callbacks, and initiative share conversation
  serialization. Approval and Stop callbacks bypass it to avoid deadlocks.
- Direct-message context is appended at execution time, preventing later queued
  messages from entering an earlier prompt. Session caches and turn counters
  now include account/channel identity, without renaming persisted sessions.
- SSE state resets before prompt submission; failed submissions surface and
  typing indicators stop on failure. Mutating HTTP calls are not auto-replayed.
- Stale tests aligned with existing English API/status names and native V2
  permissions. Legacy-import tests now use isolated state and a dummy service.
- No production database schema or configuration migration in this stage.
- Limits: conversation queue is in-memory; unified jobs/runs, durable inbox,
  multi-channel storage, general session fencing, and recovery remain pending.
- Voice transcription deferred. WhatsApp adapter not yet implemented.
- Verification: all 203 discovered tests pass; compileall passes. Real read-only
  OpenCode /api/info and Telegram getMe checks pass. Bridge restarted cleanly;
  SSE connected and @Openuna_bot reported online at 23:39:08 in the local log.
- Live post-refactor model replies have not been exercised by this verification.

## V2 approval relay correction (2026-10-30)
- Supersedes earlier claims about permission schemas: native V2 uses `permissions`
  arrays with `shell` and `subagent`. The previous V1 `permission` rewrite was wrong.
- Relay implemented in approval_relay.py with owner-only private-chat callbacks,
  bot/session binding, once-only decisions, duplicate protection and five-minute
  rejection while running. Failures never automatically approve requests.
- Uses the running server's OpenAPI: GET /api/session/{id}/permission and
  POST /api/session/{id}/permission/{requestID}/reply with `decision`.
- Session create/reuse explicitly installs V2 permission rules; new sessions set
  their location. Shell/edit require approval; subagents are disabled until
  child-session approval routing is supported. Normal bot replies stay automatic.
- 102 local unit tests pass. Real server request/list/reject contract smoke passed
  without model calls or shell execution. Telegram button clicks remain untested.
- Local commit a935f57; push blocked by missing configured GitHub credentials.
- Existing backups may still contain secrets in config, memory, database or old
  archives. File modes and tool permissions are not OS isolation.

## Fixed in first hardening pass
- Telegram inputs and callbacks require explicit owner IDs (fail closed).
- One real bot maps to its configured persona; no index-based remapping.
- Schedule execution selects the owning bot_id, not always bots[0].
- Unknown/ambiguous schedule owners are skipped.
- Same-time schedules for different bots no longer share a deduplication key.
- Automatic persona-data deletion and provisioning paused pending lifecycle review.
- Scoped process locking and stop script replace broad pkill patterns.

## Fixed in second pass (2026-10-30)
- All state moved to SQLite (`groupbot.db`, WAL) via `store.py`: schedules, outbox, seen offsets, usage.
- Failed sends queue in an idempotent outbox with capped backoff; dead after 6 attempts.
- Offset persistence per bot stops duplicate replies after restarts (was: "Halo" x4).
- Graceful SIGTERM shutdown saves context buffer snapshots.
- Scheduler v2: cron 5-field + legacy formats, constant UTC+7 evaluation. Host runs in UTC, so
  legacy minute-compare fired at the wrong Jakarta time; scheduler fixes this.
- Per-minute `last_fired` recorded in DB before firing; restarts cannot double-fire.
- Schedule buttons use durable schedule ids, no longer volatile JSON indexes.
- Budget meter: per-bot daily call + cost caps (defaults 80 calls/day, $2/day) gate model calls.

## Fixed in third pass (2026-10-30, Fase 3)
- Random group replies removed; telegram group messages only trigger a direct reply when mentioned/replied.
- Initiative loop: every `initiatif_detik` (default 300 s) the model reviews stale chats and either
  speaks briefly or answers only "DIAM" (never sent to Telegram).
- Task queue (`tasks` table) with step-capped autonomous cycle; crash recovery marks stale
  "jalan" tasks failed with reason; `/tugas` command verifies end-to-end.
- Per-minute rate limiter (sliding 60 s window, per bot, persisted) guards every model call site.
- Cron model calls now recorded into the usage meter (previously unaccounted).

## Fixed in fourth pass (2026-10-30, after Fase 3)
- chats.json and personas.json migrated to SQLite tables; chats.json archived as .imported.
- personas.json is now a mirror/inbox: the bot edits the file, the bridge syncs to DB each loop;
  a corrupt/unreadable file cancels the sync instead of mass-deleting.
- Persona deletion is appended to backup/personas.arsip.jsonl (no deletion without a trace).
- One-shot reminders: "ingetin aku (besok) jam X" creates kind=once schedules (expr in UTC,
  displayed in WIB); kind=interval also served via due_schedules.

## Fixed in fifth pass (2026-10-30, Fase 0/5 prep)
- Tokens out of config.json plaintext: `{file:<nama>.token}` indirection resolved at startup;
  provisioning writes secrets to per-bot token files, never back into config.
- Daily state backup (tar.gz, keep 7): consistent DB copy via sqlite backup API + config,
  personas mirror, memories; verified end-to-end.
- Provisioning flow is owner-gated by Telegram buttons (pv-y-/pv-n-); automatic spawn removed.
- Persona deletion now archives the memory file to backup/memori/ before removal.

## Fixed in sixth pass (2026-10-30, "feel like Grok" UX package)
- Streaming Stop: ⏹ button on streamed messages; tries OpenCode `POST /session/:id/abort`,
  falls back to bridge-side stop flag (endpoint 404 on OpenCode v2.2 → fallback is authoritative).
- Clipped replies: nothing silently truncated at `max_char_reply` — ▶️ Lanjut chain (3500 char/chunk).
- Task queue visibility: claim ack (🫡 budget N steps) + progress ping every 3rd step (⚙️ x/y).
- `/new`: server-side compact + auto handoff summary (max 120 words) carried into the fresh session;
  `/status` and `/reset` now keyed consistently by (persona, chat, mode).
- Auto-compact: private chats compact after `compact_per_turns` (120) turns via `POST /session/:id/compact` (verified 200 on v2.2).
- Media janitor: `media/` files older than 7 days removed in the daily backup sweep.
- Telegram rich text: `parse_mode Markdown` + safe plain fallback on HTTP 400 "can't parse entities";
  streaming final edit renders markdown, interim edits stay plain.
- Deployed live (PID 29023) and pushed upstream: commit `3d57106`, CI success; 87 unit tests green.

## Fixed in seventh pass (2026-10-30, external review triage)
Reviewed at commit 9bf944d by an outside reviewer; findings resolved as follows:
- `/reset` stale 2-tuple session key → confirmed real at 9bf944d:1300, already fixed in
  `3d57106` (3-tuple `(nama, chat_id, mode)`); now locked by a regression test.
- `/status`–`/reset` crash with `persona=None` → partially wrong: the `persona or {…}`
  fallback predates the review (guard at 9bf944d:1244). Live-tested against the real bot;
  regression test added.
- Attachment download before owner check → confirmed real; **fixed**: authorization now
  precedes `ambil_lampiran` (source-order regression test guards it).
- opencode.jsonc `permissions` (plural, array) → confirmed real; OpenCode only reads the
  singular `permission` OBJECT keyed by tool name, so every deny rule was dead. Config
  rewritten to the real schema (read/edit/bash/external_directory with pattern maps).
- Bot token + `pool.json` (spare tokens) inside daily backups → fixed: credential files
  excluded from the archive; `*.token`/`pool.json`/`provision.json` chmod 600 at startup.
- README claim "destructive actions require owner confirmation" overstated → reworded
  (only provisioning has a confirmation button).
- Known remaining (by design for now): agent shell is allow-by-default with deny rules only
  (no bash `ask` relay yet — needs a Telegram Approve/Deny bridge before it works headless);
  agent runs in the bridge user's environment (OS sandbox/separate user is Fase 4 work);
  natural-language routine authoring still interests the model's own writing of cron.json.

## Not implemented / remaining risks
- No VM, container, filesystem sandbox, per-bot OpenCode service or credentials.
  All agent tools still run with the shared service's host permissions.
- Owner-only access is a bridge boundary, not protection from prompt injection.
- Model/agent callbacks still use volatile indexes; stale buttons need durable IDs.
- Session polling can capture partial text and concurrent turns can cross replies.
- Per-bot library isolation is not implemented; budget caps are enforced in the bridge only.
- Deleting a persona does not delete a Telegram bot account.
- Tokens previously appeared in conversation; rotate using BotFather.
- `chats.json` was migrated to SQLite; personas.json still receives model writes via file inbox
  (mirrored to DB, archived on delete). One-shot reminders now have a live chat trigger.

## ✅ Resolved (was open)
- ~~No per-minute rate limits~~ → rate_limiter.py (60 s window, per bot, persisted).
- ~~Budget caps~~ → daily cap (80 calls/$2) + per-minute cap.
- ~~Personas deleted without archive~~ → arsip .jsonl jejak.
- ~~chats.json/personas.json remain JSON~~ → migrated (personas file kept as inbox mirror).
- ~~One-shot schedules have no UI trigger~~ → "ingetin aku (besok) jam X" in chat.
- Initiative behavior is untested against a real group (current tests: private chat only).
- Bot tokens still plaintext in config.json; rotation pending owner action via BotFather.

Do not expose the service publicly or accept arbitrary group members' commands.
Next phase: task queue + [DIAM]-default initiative layer, then OS-level isolation
on a suitable host. Runtime provider availability must be verified, not assumed.

## ✅ Deploy record — 2026-10-31 06:27 UTC+7: routine engine (v0.3.0)

Changes: per-item routine management in natural language (pause/resume/edit/
delete-one), weekly cron routines from plain language, /jadwal panel with ids
and paused states, ambiguous "batalkan jadwal" now asks instead of wiping all,
explicit "batalkan semua jadwal" required for a wipe. Store gains
get_schedule/set_schedule_active/update_schedule_expr — no schema change
(migrations stay 1/2/3).

Verification after deploy:
- `./stop.sh` clean drain; supervision restarted, bridge online (offset 19758731).
- Telegram getMe → OK @Openuna_bot; 13 commands registered.
- SQLite quick_check ok, foreign_key_check empty, migrations [(1,), (2,), (3,)].
- 8 succeeded jobs preserved.
- Tests: 366 OK (341 prior + 25 new in test/test_routines.py); compileall clean.
## ✅ Deploy record — timezone hardening for one-shot routines

Found via dual-TZ review: stored one-shot expressions are UTC-naive, but the
firing evaluation interpreted them as host-local time — reminders would have
fired 7 hours early on TZ=Asia/Jakarta hosts (production TZ=UTC happened to
mask it). Fix: triggers.py now reads one-shot exprs as UTC explicitly;
test_routines uses an aware fixed clock so expectations are TZ-independent;
test_background_runtime stores the overdue-one-shot in UTC convention;
CI now reruns the full suite under TZ=Asia/Jakarta.

Verified: full suite OK under TZ=UTC, TZ=Asia/Jakarta, and TZ=America/New_York.
Bridge restarted via ./stop.sh + supervise.sh after the fix: single supervisor,
single poller, Telegram bot online (offset 19758731), daily backup saved.

## ✅ Live reminder E2E check — 2026-10-31 06:51 UTC+7 (explicit "BELUM DICEK" item)

Method: a one-shot schedule (id 1, once, prompt "Say exactly this: ⏰ LIVE-CHECK")
was inserted directly into the durable `schedules` table with expr
"2026-10-31 06:51" (UTC-naive, real target 06:51:00) while the bridge ran.

Result:
- **Trigger timing verified on time**: trigger row + job #9 created at
  epoch 1793429469.7 = 06:51:09.7 — 9.7 s after the target (one pump tick).
  The UTC reading of stored one-shot expressions works live.
- **Content delivery failed — OpenCode backend quota, not the bridge**: the
  scheduled session's idle outcome was `failed` with provider error
  `provider.chatgpt-limit` ("ChatGPT usage limit reached"). run marked the job
  `failed` durably, and the standard failure notice ("⚠️ Job #9 failed. Use
  /jobs for status.") was delivered to the chat.
- The durable safety net behaved exactly as documented: terminal failure,
  no replay, owner notice sent.
- Improvement candidate: classify provider-limit outcomes as RetryLater
  (quota pause, deferred re-execution) instead of a terminal failure, since
  the run shows zero tokens (the prompt never executed).

Cleanup: schedule row 1 deleted after the check.

## ✅ Deploy record — provider-limit quota pause (same day, 07:12 UTC+7)

Follow-up on the live check: job #10 (owner's own chat message "Bos") also
failed with the same backend `provider.chatgpt-limit`. Classification landed:
idle outcomes reporting provider limit/quota now raise RetryLater(900) — the
job defers for 15 minutes and resumes; ordinary provider failures still fail
durably. TDD: 3 new classification tests added first (test_opencode_completion
needs RetryLater), then implemented; suite 369 OK, all green.

Deploy: ./stop.sh + supervise.sh restart; single supervisor/poller; bot online
offset 19758732. Note: until the OpenCode-side quota resets, deferred jobs
will re-attempt every 15 minutes and keep parking quota pauses durably.

## ✅ Batch 2 — queued-job cancellation (07:3x UTC+7)

Adds `/cancel <id>` (owner private chat) plus a Cancel button on `/job <id>`
for queued jobs. Durable: only status='queued' (incl. quota-deferred) flips to
'cancelled'; a running claim wins and refuses. The dispatcher is collision-free:
cancelled jobs claim nothing and exit. TDD: test_job_cancel.py (9 tests) written
first, then store + JobCommands + gateway routing + `jx-` callback. Suite now
378 OK; compileall clean.

## ✅ Batch 3 — SQLite long-term memory (07:3x UTC+7)

Owners teach durable facts in chat: /remember <fact>, /memory (ids listed,
chat facts before global), /forget <id> (deactivates; the row stays as audit
trail; forgetting is bot-scoped). Active memories are injected automatically
into every conversation briefing's PERSISTENT MEMORY section with a
never-contradict rule. Additive schema: `memory(id, bot_id, chat_id, text,
active, created_at)` via CREATE TABLE IF NOT EXISTS (legacy DBs migrate on
boot). TDD: test_memory.py (6: store roundtrip, empty-text ValueError, global
facts visible in any chat, limit cap, briefing rendering) and
test_memory_commands.py (7: command wiring, unknown/foreign id rejection,
bot scoping) written first. Suite 391 OK; compileall clean. Live-verified:
reload healthy (getMe @Openuna_bot, OpenCode 2.2.4, quick_check ok, migrations
3, succeeded jobs 8, memory table present).

## ✅ Batch 4 — outbound files via sendDocument (07:5x UTC+7)

Background agents can now hand produced files straight into the chat. A final
message with a marker line `@file hasil/name.ext` (any file) or
`@photo hasil/img.png caption` (image) is split by parse_file_outputs into a
clean text plus durable document/photo delivery operations; the task and
work-mode prompt teach the convention. New pipeline pieces: OutputService.
send_file (durable when a job is in scope), TelegramDelivery.deliver_file with
a stdlib multipart uploader (uploads.py, no external dependency), and a strict
outbound_path guard — only existing files strictly inside the bridge home are
sendable, so `../` escapes and absolute paths can never leak. Outcome
classification follows the text path (sent/dead/pending/uncertain).

TDD: test_file_out.py (12 tests) written first: marker parsing, path-safety
rejections (`../`, absolute, missing), multipart encoding, delivery outcomes,
OutputService scoping, and executor-level marker→file emission. Suite 403 OK.

Live E2E: a document delivery for `hasil/tes-file-terkirim.txt` (caption
"Tes batch 4 — kirim file outbound") was injected as pending and confirmed
`sent` by the running poller within seconds — the owner received the file on
Telegram. Test stub rows (inbox 999900, job 999901, delivery 11) were deleted
afterwards; foreign keys clean, succeeded jobs back to 8, test file removed.
Reload healthy at 07:48 (single supervisor/poller, bot online offset 19758732).

## ✅ Batch 5 — untrusted-content markers (07:5x UTC+7)

User messages and attachments now reach the agent wrapped as
`⟦UNTRUSTED MESSAGE FROM <sender>⟧ … ⟦/UNTRUSTED⟧` (attachments:
`⟦UNTRUSTED ATTACHMENT FROM <sender>⟧`) with a fixed data-not-directives note.
`framed()` neutralizes marker tokens embedded in user text, so spoofing the
closing marker is impossible. Routing is untouched: commands and schedules
still match on the raw text; the framing happens at prompt-build time. The
briefing teaches the rule once, globally.

TDD: test_untrusted.py (3 tests) written first (framing shape, marker
neutralization, answer-prompt integration); one legasi assertion in
test_bridge_dispatch was updated to the new prompt shape, the rest untouched.
Suite 406 OK; compileall clean. Reload healthy (getMe @Openuna_bot,
OpenCode 2.2.4, quick_check ok, fk clean, succeeded jobs 8).

## ✅ Batch 6 — MCP connector template (07:5x UTC+7)

deploy/mcp.example.jsonc documents mergeable opencode mcp entries for Gmail
(@gongrzhe/server-gmail-autoauth-mcp), Google Drive
(@modelcontextprotocol/server-gdrive), and GitHub
(@modelcontextprotocol/server-github) — all disabled by default, secrets
referenced through {file:*} indirection into 0600 private files, with merge
and setup notes in-file. README gained a "MCP connectors (optional)" section;
config.example.json links the template. No runtime change: nothing is enabled
until the owner merges and flips enabled:true.

TDD: test_mcp_template.py (4 tests) written first (three servers present,
type/enabled declared, no live credentials in the template, documented
GITHUB_PERSONAL_ACCESS_TOKEN env name). Suite 410 OK; compileall clean.
No reload needed (config-only change); running bridge untouched.

## ✅ Deployment v0.4.0 + typing indicator (08:1x UTC+7)

Tag v0.4.0 created over the six hardening batches (a8dcabe…64587f1) and pushed
with main. Release commit 381f005 (CHANGELOG restructured: the batch items moved
into their own [0.4.0] section). Owner report "typing indicator disappeared":
verified by log + code reading that sendChatAction was never implemented — the
indicator had never existed, messages themselves did arrive (jobs #13–16 all
succeeded and confirmed deliveries). Added gateway.typing_action: a daemon
thread sends sendChatAction "typing" every 4 s from message-ack until the reply
path leaves execute(), best-effort (channel failures end the cosmetic loop,
never the job); commands return early and are unaffected.

TDD: test_typing.py (3 tests) written first (loop sends typing and stops,
survives a failing channel, execute triggers typing before answer). Suite 413
OK; compileall clean. Reload healthy (single supervisor/poller, bot online
offset 19758739).
