# Bridge audit

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
