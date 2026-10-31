# OpenBridge architecture

## Current composition boundary

`bridge.py` is a seven-line entry point importing `openbridge.app.main`.
Importing it, the bootstrap, or any package module performs no configuration,
database, network, or thread startup. `bootstrap.create_application()` explicitly
loads/builds per-instance services and owns an ExitStack for storage cleanup.
`Application.run()` is the only normal startup path for worker/poller loops.

The root script was not moved wholesale: responsibilities are separated into
Telegram ingress/commands/media/approval adapters; conversation, persona,
provisioning, schedule, maintenance, and routing services; and explicit assembly.
Different application instances do not share session caches, model selections,
approval routes, queues, connections, or mutable configuration.

The existing storage/limiter/context/approval Python libraries remain side-effect
free compatibility dependencies. Their state is created only in the factory.
Database schema and existing launch/state paths are unchanged by this extraction.
Earlier sections below record the incremental migration history; statements about
an eager monolithic bridge describe previous stages, not the current entry point.

### Safety and compatibility details

- Ingress rechecks authorization before downloading attachments or executing
  saved messages. Output and job controls retain channel/account/conversation scopes.
- Commands/callbacks use injected services. Model/agent menus are account-scoped;
  invalid/negative indices fail closed. Stop checks registered session ownership.
- Provisioning stays disabled by default. Approval is private, account-bound,
  once-only, and uses opaque short callback keys. Unknown token identities leave
  the pool unchanged; existing credential files are never overwritten. Runtime
  and source configuration keep credentials separate via file references.
- Persona removal is archived; invalid persona documents cannot mass-delete rows.
  Unowned/invalid schedule inbox entries are retained for review, not discarded.
- JSON updates and backups use private temporary files and atomic replacement.
  Attachment downloads have a real byte cap and account/chat-scoped private paths.
- Conversation/background session mechanics stay separate. `/new` archives the
  old session lookup title before opening a fresh context, instead of silently
  finding the same old session again. Failed reset deletion keeps the old binding.
- Obsolete SSE/token-streaming code had no active callers after durable final
  delivery was introduced; it has been retired, not moved into a hidden monolith.
  Completion still follows the accepted user message through terminal idle.
- Factory failure closes already-created connections. Application shutdown only
  closes storage after runtime/worker/auxiliary activity has stopped; uncertain
  in-flight work remains recoverable under the existing reconciliation policy.

Still pending: cross-channel identity/context/persistence migration, WhatsApp
adapter, and OS isolation. Reminder lanes landed without schema changes:
schedule-kind jobs pass the same-lane conversation leader (not the interrupted
fence), use a dedicated in-memory dispatcher key, and deliver first inside the
background tier. Settled jobs age out after `retention.days`; fenced/ambiguous
work stays for owner review. Process supervision runs under `supervise.sh` on
this host (systemd unit available in `deploy/`).

OpenBridge is an autonomous agent runtime, not a Telegram bot framework.
OpenCode provides execution; channels provide communication with people.
Use English for new identifiers, comments, tests, and documentation. Replies
may follow the user's language. Preserve legacy persisted names until an
explicit, tested migration is available.

## Direction: modular monolith

```text
Telegram / WhatsApp / CLI     Scheduler / external triggers
             \                 /
               incoming events
                      |
             identity and policies
                      |
              runtime dispatcher
                      |
                  job queue
                      |
               bounded workers
                      |
               execution port
                      |
               OpenCode adapter

SQLite: durable inbox, jobs, runs, checkpoints, approvals, outbox
Channel adapters: notifications, approvals, and result delivery
```

No distributed broker or microservices are required for the initial ARM64
phone deployment. One persistent Una identity can own many jobs; workers are
bounded executors, not automatically separate agents or AI supervisors.

## Concepts

- **Agent**: identity, instructions, defaults, and policies.
- **Conversation**: channel + account ID + conversation ID, never a credential.
  Cross-channel user identity links must be explicitly authorized.
- **Job**: requested goal, whether triggered by a message or schedule.
- **Run**: one attempt to execute a job; retries create new attempts.
- **Worker**: a bounded executor.
- **Session**: execution context in OpenCode. Chat contexts may be reused;
  independent jobs should have independently scoped contexts.
- **Delivery**: sending an already-produced result, independent of execution.

## Implemented: stage 1

```text
openbridge/
  domain/messages.py                # Neutral identity/message models
  ports/                            # Channel, execution, and JSON contracts
  runtime/dispatcher.py             # Bounded, keyed FIFO executor
  adapters/
    http.py                         # JSON transport
    channels/telegram/adapter.py     # Telegram normalization and wire format
    execution/opencode/client.py     # Session/prompt operations
```

Domain and runtime code do not import adapters. The package can be imported
without loading user configuration, opening a database, starting threads, or
contacting external services. `bridge.py` remains the legacy composition root;
its import still initializes legacy state, so new runtime code must not import it.

### Execution rules

- Direct messages, slash commands, non-control callbacks, and initiative share
  per-conversation FIFO execution, with concurrency between conversations.
- Context is appended when a direct turn starts, not when polling ahead.
- Approval and Stop callbacks bypass the queue to prevent approval deadlocks.
- Worker and queue limits include running work; overload produces a message.
- Session cache keys and turn counters include account/channel identity.
  Existing session titles remain unchanged for restart compatibility.
- Stream state resets before prompt submission. Submission failures surface
  immediately; typing indicators stop even when execution raises an exception.
- HTTP reads may retry connection failures. Mutations are not automatically
  replayed because a disconnected request may already have executed.

Optional settings in `config.json`; existing configurations work unchanged:

```json
{
  "runtime": {
    "interactive_workers": 1,
    "background_workers": 1,
    "max_pending_messages": 128,
    "max_pending_per_conversation": 32
  }
}
```

The queue is **in memory**, not a durable inbox. A crash can lose accepted work.
Shutdown attempts a 20-second drain; unfinished work is not replayed. Existing
Telegram offset semantics are unchanged, so exactly-once processing is not
guaranteed. A timeout does not prove that an OpenCode run stopped.

### Compatibility

This stage does not rewrite production configuration or database schemas.
Legacy files and field names remain valid. `OPENBRIDGE_HOME` optionally selects
the configuration/state directory; its default is the directory containing
`bridge.py`. `OPENBRIDGE_SERVICE_JSON` still selects the OpenCode registration.
Tests importing the legacy bridge use temporary state and a dummy service.

## Implemented: stage 2 (supersedes stage 1's in-memory-only limitation)

`adapters/persistence/sqlite/jobs.py` owns versioned, additive `runtime_*` tables;
`runtime/jobs.py` coordinates durable execution and independent delivery.

- One transaction stores an incoming Telegram event, its job, and its cursor.
  Deduplication uses channel/account/source-update identity. Unauthorized input
  only records an empty receipt and cursor, never work or downloaded content.
- A private-chat acknowledgement is attempted only after acceptance commits.
  Its message ID is retained; final delivery edits that message when possible.
  New jobs use received/processing/final status, not token-by-token streaming.
- Jobs are durable before entering the bounded in-memory dispatcher. Saturation
  delays dispatch rather than dropping accepted work. Each conversation exposes
  only its oldest queued job, preventing one busy conversation from filling the
  entire dispatch window.
- Each execution has a run record. Session checkpoints are persisted before
  prompting, including the initial briefing. Results and outbox intents commit
  in one transaction after execution. Bot credentials are resolved at delivery
  time, not copied into these new runtime tables.
- OpenCode prompt receipts are correlated by user message ID. Histories are
  sorted chronologically (the real service returns newest-first). Completion
  requires the subsequent terminal idle event; partial text is not success.
- Restart preserves queued work but marks running attempts interrupted. The
  conversation is fenced until explicit owner reconciliation. A known active
  session or nonempty backend inbox blocks reconciliation. With no checkpoint,
  the owner's explicit confirmation is the only reconciliation evidence.
- Stop and approval callbacks bypass ordinary jobs, are not automatically
  replayed, and remain owner-only. Stop uses the documented V2 interrupt route.
- Delivery retries never invoke execution. Known-message edits can be retried;
  a send whose outcome is unknown becomes `uncertain`, not blindly resent.
  HTTP 429 honors Telegram's retry delay. Permanent failures become dead letters.
- `/jobs` includes delivery state; `/result <id>` retrieves saved output without
  model work. `/resolve <id> confirm` cancels an interrupted attempt after review
  and releases its queued successors. It does not retry the interrupted job.

Recovery runs once under the single-poller process lock. This is a single-runtime
design, not a distributed lease system. Exactly-once external effects are still
not claimed. Legacy cron/tasks/initiative retain their original storage/lifecycle;
initiative respects conversation fences. Rich output schemas still have Telegram
details, so enabling another channel requires the next extraction/migration stage.
Runtime inbox/results may contain private messages: keep databases/backups private
and plan retention before exposing the service to more accounts.

## Implemented: stage 3 — unified background execution

This supersedes stage 2's legacy cron/task/initiative execution limitation.
`services/triggers.py` produces work; `services/background.py` executes it behind
injected session/model/delivery boundaries.

- Migration 2 adds typed triggers, safe deferral timestamps, completed task-step
  checkpoints, and persisted initiative generations. Existing schedule/task rows
  remain authoritative compatibility records, not duplicated copies.
- Schedule firing marks and job creation commit atomically. Deduplication keys
  identify schedule occurrences, task IDs, and initiative generations. Internal
  inbox IDs occupy a reserved negative range and never advance Telegram cursors.
- Cron uses UTC+7 without a second host-local fallback. Once/interval timestamps
  retain their original host-local interpretation. Overdue one-shots fire once;
  missed cron minutes are not backfilled. Disabled/deleted schedules have queued
  occurrences skipped. Duplicate entries in one legacy import are merged.
- Tasks are linked before being claimed. Their running/final projections update
  transactionally with runtime state. Unlinked legacy running tasks are failed
  conservatively, without discarding linked queued/deferred work.
- Background jobs have independent sessions and native tool permissions. Model
  calls pass usage limits. Known-safe quota pauses produce deferred runs and
  resume from completed steps; crashes/ambiguous calls still require review.
- Tasks need a DONE/SELESAI report to succeed; FAILED/GAGAL or step exhaustion
  means failure. A model's report is not independent proof that an external
  goal was achieved; prompts require evidence, but operators must still review it.
- `/job` aliases `/jobs`. `/job <id>` shows task progress and Stop for a known
  running session. `/result <id>` can retrieve a checkpoint, labeled non-final.
- All kinds retain per-conversation FIFO execution. Long tasks can delay normal
  replies in their conversation; job controls, Stop, and approvals bypass it.
  Priority lanes and distributed leases are not implemented.

Old direct cron/task execution and memory-only initiative dispatch were removed.
Results/notifications use the durable outbox. The legacy outbox still serves
old/direct responses outside jobs and needs channel-neutral extraction.

## Follow-up stages

### Implemented: reserved execution lanes

Migration 3 assigns jobs to `interactive` (Telegram messages/commands/callbacks)
or `background` (tasks/schedules/initiative), without changing their statuses,
cursors, results, or trigger identities. This supersedes stage 3's single FIFO
for an entire conversation.

- Each lane has a separate bounded worker pool: one chat worker and one
  background worker by default. A background job cannot occupy the chat slot.
  `interactive_workers` and `background_workers` configure the pools. Legacy
  `conversation_workers` is a fallback for interactive capacity only. Queue
  limits apply independently to each pool, not as one combined memory limit.
- FIFO ordering and at-most-one active run apply per channel/account/conversation
  **and lane**, both in SQLite claim checks and dispatcher keys. Chat turns still
  share their conversation session sequentially; background jobs retain their
  independent, checkpointed sessions. Different lanes may execute concurrently.
- Dispatch queries use an independent limit for each lane, preventing an older
  background backlog from hiding newer chat jobs outside the selection window.
- Ready chat deliveries are preferred over ready background deliveries; chunk
  ordering within each result is unchanged. An already-running network request
  cannot be preempted, and heavy chat traffic can delay background delivery.
- An interrupted job fences new work in **both** lanes until owner reconciliation.
  Already-running work in the other lane is not automatically aborted.
- Shutdown closes both pools under one drain deadline and does not claim queued
  work after stopping. Queued jobs remain durable for the next startup.

These are execution reservations, not provider-quota reservations or OS isolation.
Rate limits, service availability, Telegram latency, and host filesystem access
are still shared. Different sessions can act on the same files; approval prompts
do not provide file-conflict locking. Tasks, reminders, and initiative still share
the background FIFO, so a long task may delay a reminder, but not ordinary chat.

1. **Execution policies**: add explicit owner-requested retry/new-attempt,
   retention, weighted delivery fairness, and cancellation policies without
   replaying uncertain work. Consider deadline-aware reminders separately.
2. **Recovery tooling**: improve reconciliation UI and saved-result inspection.
   Distributed leases are needed only if multi-process execution is introduced.
3. **Runtime extraction**: move polling, commands, media, sessions, SSE,
    lifecycle, and remaining maintenance loops out of `bridge.py`.
4. **Identity/storage migration**: replace chat-ID-only context and legacy
   task/schedule/outbox addressing with account/channel scopes. Scope private
   memory explicitly. Complete this before enabling another channel.
5. **WhatsApp adapter**: choose a supported gateway, authenticate webhooks,
   deduplicate events, and map capabilities. Use plain-text fallbacks where
   editing, buttons, or streaming are unavailable.
6. **Operations**: process supervision, health checks, structured job/run logs,
   and OS isolation. The conversation queue cannot restart a dead process.

### Implemented module boundaries

- `config.py`: explicit paths, configuration loading, credential indirection,
  worker settings, and sanitized registration errors. It preserves source files
  and unknown fields; resolved credentials are excluded from configuration repr.
- `app.py`: injected lifecycle without backend/channel imports. It owns signal
  installation/restoration, loop/poller startup, a per-account poller registry,
  worker-pool drain under one deadline, and context-save cleanup. Provisioning
  uses the same registry as polling supervision to prevent duplicate consumers.
- `adapters/channels/telegram/polling.py`: identity/command registration and
  polling, with injected durable acceptance and storage. Shutdown after a long
  poll leaves unaccepted updates at their source cursor.
- `services/delivery.py`: captures output during a run and enforces destination
  scope; otherwise delegates through the `DeliveryChannel` port. Telegram wire
  formatting, retry classification, and legacy fallback live in its adapter.
  Plain-text fallback requires an explicit HTTP 400 parse rejection, not an
  ambiguous upstream response mentioning parsing.
- `services/sessions.py`: conversation cache/attachment and independent job
  sessions. Registration/checkpoint precede model calls. Native session listing,
  reuse, permission enforcement, and idle checks are OpenCode adapter operations
  behind the execution contract. An unavailable session list cannot silently
  create a duplicate session.
- `services/briefing.py`: a separately testable compatibility template; it still
  describes legacy Telegram personas/files and is not a new multi-channel policy.
- `services/job_commands.py`: channel/account/conversation-scoped controls using
  the backend's idle contract, not native API routes. Authentication remains the
  channel ingress's responsibility; the controller is a trusted internal boundary.

None of these modules imports `bridge.py` or initializes user state at import.
The root script still performs eager legacy bootstrap and binds compatibility
wrappers; regular commands/callbacks, persona/provisioning, maintenance, and some
stream handling remain to extract. It is deliberately not described as a thin
entry point yet. Complete those extractions and an explicit composition factory
instead of moving the remaining monolith wholesale to another filename.

## Remaining boundaries

- The low-level Telegram adapter and basic OpenCode operations are extracted;
  rich delivery, polling, SSE, and approval orchestration still have legacy code.
- Legacy schedule/task execution now uses the durable runtime. Their storage,
  legacy outbox, and rich Telegram outputs still need channel-neutral extraction.
- Per-lane conversation serialization is not OS isolation. Concurrent lanes and
  different conversations can share a workspace and service user's host access.
- Owner checks and permissions do not provide OS isolation. OpenCode tools run
  with the shared service user's host access.
- WhatsApp and voice transcription remain unimplemented. Stage 2 adds crash-safe
  acceptance for normal Telegram jobs, not automatic replay of approval controls.

## Verification

```sh
python3 -m unittest discover -p 'test_*.py'
python3 -m compileall -q bridge.py openbridge
```

Fake-service integration covers queued Telegram turns and context isolation.
Unit tests cover adapter contracts, bounds, cancellation, failure, and shutdown.
Stale duplicate tests now use existing English APIs/statuses; permission tests
assert native V2 rules rather than the V1 schema. No model invocation is needed.
