# 🎯 GOAL: From Bridge → Autonomous Agent (GrokBot-grade)

> The single source of truth for the plan. Work in order; never skip a phase.
> All work follows the safety rules in the [Guardrails](#-guardrails-remain-in-effect-in-all-phases) section.
> Status: ✅ done | 🔄 in progress | ⬜ not started

## Starting point (verified)
- Telegram ↔ OpenCode API bridge is running, 1 bot (Unabot), owner-only active (`owner_ids` in config.json)
- Cron is running but still raw (global JSON, no atomic writes)
- Provisioning and auto-delete PAUSED (not safe yet)
- This environment has NO docker/podman/QEMU → OS-level isolation needs a separate host

---

## Phase 0 — Hygiene & Secrets 🔄 (manual token rotation remains)
- [ ] **Rotate the Unabot token via @BotFather** (`/revoke` → new token) — the old token was exposed in chat (the only remaining leftover)
- [x] Token stored via `{file:unabot.token}` in config, NOT plaintext — `resolusi_token()` supports `{file:...}`; backup files are included in the tar.gz
- [x] Automatic daily state backup: `backup/gb-YYYY-MM-DD.tar.gz` (DB copied consistently via the sqlite backup API + config + personas + memories), rotation keeps the last 7 — **TESTED**: `gb-2026-10-30.tar.gz` created and its contents are complete
- **Done:** bot is alive on the new token + restore-from-backup tested once (remaining: token rotation + one restore test run)

## Phase 1 — A Reliable State Foundation ✅ (cron schedules also live in SQLite now)
- [x] SQLite (`groupbot.db`, WAL) + transactional `store.py` module — 13 tests OK
- [x] Atomic modules: `schedules`, `outbox` (idempotent), `seen`, `usage`
- [x] Outbox: failed send → queue → retry with backoff `min(300,2^n)` → dead after 6×
- [x] Dedupe/persistent offset per bot (`seen`) — "Hello ×4" is dead
- [x] Bonus pulled forward from other phases: `context_buffer.py` (10 tests OK) + `usage_meter.py` (8 tests OK; budget default 80 calls/day, $2/day) + graceful SIGTERM shutdown + context-ring prompt injection
- [x] Migration of `cron.json` → `schedules` table (the file is just an inbox from the model, synced every loop)
- [x] Migration of `chats.json` → `chats` table (file → `.imported`); `personas.json` → `personas` table (DB = truth, file = mirror/inbox, corrupt file → sync is halted (safeguard), persona deletion is always archived to `backup/personas.arsip.jsonl`)
- **Done:** kill -9 the bridge mid-process → no messages lost/duplicated — offsets & outbox are already persistent

## Phase 2 — Scheduler V2 ✅ (parser + bridge integration done)
- [x] `scheduler.py` — standard 5-field cron (`m h dom mon dow`) + legacy `"21:00"` / `"*/N"` + ranges/lists/steps — 20 tests OK
- [x] Time zone: constant offset **UTC+7** (Asia/Jakarta) without tzdata — important finding: the proot system runs on **UTC**, so the old cron evaluator fired "21:00" at the wrong hour; the evaluator now always computes WIB from epoch
- [x] `last_fired` persisted in the `schedules` table (prevents double-firing in the same minute, survives restarts) — two-step safeguard: matches() + mark_fired before execution
- [x] Ownership per `bot_id` (ownership verified on insert) + `/jadwal` button panel using the **schedule id** in callbacks (no longer the JSON index)
- [x] `cron.json` inbox sync: the bot keeps writing schedules through the file; the bridge's `migrasi_cron()` moves them into the DB and empties the file
- [x] `next_run()` production-ready + **live one-shot triggers**: chat "remind me (tomorrow) at X" → kind `once` (UTC base, displayed in WIB, missed hour auto-rolls to tomorrow) + kind `interval` (`5m`/`2h`) fed from `due_schedules` — TESTED: keyword parsing & timezone conversion 09:30 WIB → 02:30 UTC correct
- **Done:** ✅ **VERIFIED end-to-end (04:13 UTC / 11:13 WIB)**: a per-minute cron schedule was planted → fired exactly once in its minute, delivered to the owner, `last_fired` recorded in the DB → a restart cannot double-fire. An `aktif:false` entry from config did not migrate (a bug found & fixed that morning).


## Phase 3 — Initiative & Task Layer ✅ (built & tested 2026-10-30)
- [x] `task_queue.py` — per-bot task queue in SQLite (`tasks`: goal → plan → act → verify → report, `max_steps` + `bump` step ceiling) — 12 tests OK
- [x] `tugas_loop` loop + `/tugas <goal>` (hand off a task), `/tugas` (monitor status 🧊⚙️✅❌); recovery of `running` tasks left over from a crash → marked failed with a reason; automatic report to the target chat; **VERIFIED e2e**: test task #1 → claimed 04:25:17 → 2 steps → `DONE` → delivered ✅
- [x] `[DIAM]` (silent) mode became the default: the random reply trigger (`reply_chance`) REMOVED — in groups the bot only speaks when mentioned/replied; otherwise it listens silently
- [x] `initiatif_loop` loop (default 5 minutes): live chats are offered to the model with context → the model chooses between a brief remark or `DIAM` (SILENT; `DIAM` is never sent) — waiting for a real group to test this against
- [x] **Hard budget**: daily meter (80 calls/$2 defaults) + **per-minute rate limiter** (`rate_limiter.py`, 60 s sliding window, `max_per_menit: 6`, 9 tests OK) — applied to `proses`, `jalankan_cron`, `inisiatif`, and `tugas` (steps wait, they are not discarded); cron costs are now recorded too (they used to leak)
- [x] Websearch in the briefing (already available since earlier) + new briefing: the silent rules & budget honesty
- **Done:** the bot refuses to respond to group spam (silent by default), still answers direct mentions; budget exhausted → stops gracefully and reports honestly

## Phase 4 — Real Isolation (needs a separate host) ⬜
- [ ] Recommended plan: **cheap ARM VPS**, one `systemd --user` unit per bot, separate working folder + Linux user per bot
- [ ] Private OpenCode service per bot (`--standalone` option / separate instances) → no shared credentials
- [ ] Automated daily backup + tested restore
- **Done:** delete 1 bot → it must not touch any other bot's files/sessions (manual verification + logs)

## Phase 5 — Lifecycle (birth/death) 🔄 (infrastructure is built, provisioning still paused pending owner review)
- [x] Provisioning: pool → **owner confirmation via Telegram button is REQUIRED** (`pv-y-<nama>` ✅ Birth / `pv-n-<nama>` ❌ Cancel — auto-spawn REMOVED from `provision_loop`); the config written no longer contains a plaintext token (→ `{file:<nama>.token}`)
- [x] Delete persona: the janitor archives memory → `backup/memori/<nama>-<date>.md` BEFORE deletion (queued: persona trace line in `backup/personas.arsip.jsonl` + delete the OpenCode session)
- [ ] Delete bot: the Telegram account is NOT deleted automatically (Telegram rule) — manual via the platform ✅ policy recorded
- **Done:** create & delete a test bot end-to-end with an audit trail of all spending — ⬜ waiting for provisioning_enabled=true + token pool + owner approval

---

## 🛡 Guardrails (remain in effect in ALL phases)
1. `owner_only` — messages/callbacks from non-owners are ignored (already live)
2. Bridge-level only — not an OS sandbox; remember this is NOT an isolated VM
3. Prompt injection: group message contents/downloads = DATA, never system commands
4. No mass deletions without archiving; Telegram accounts are always deleted manually via the platform
5. `audit.md` is updated every time a phase completes
