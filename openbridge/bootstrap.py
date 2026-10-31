"""Explicit composition root. Only create_application opens user state."""
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
import threading
import time

from context_buffer import ChatBuffer
from usage_meter import UsageMeter
from rate_limiter import RateLimiter
from store import Store
from task_queue import TaskQueue
from approval_relay import ApprovalRelay
from runtime_policy import authorized, bot_id
from scheduler import matches

from openbridge.app import Application
from openbridge.config import Paths, load_config, load_registration, ConfigurationError
from openbridge.infrastructure.logging import FileLogger
from openbridge.adapters.http import JsonHttpClient
from openbridge.adapters.execution.opencode import OpenCodeClient
from openbridge.adapters.persistence.sqlite import JobStore
from openbridge.adapters.channels.telegram import TelegramClient
from openbridge.adapters.channels.telegram.delivery import TelegramDelivery
from openbridge.adapters.channels.telegram.media import TelegramMedia
from openbridge.adapters.channels.telegram.commands import TelegramCommands
from openbridge.adapters.channels.telegram.gateway import TelegramGateway
from openbridge.adapters.channels.telegram.approvals import notify_approval
from openbridge.runtime.dispatcher import ConversationDispatcher
from openbridge.runtime.jobs import DurableRuntime
from openbridge.services.delivery import OutputService
from openbridge.services.job_commands import JobCommands
from openbridge.services.personas import Personas
from openbridge.services.schedules import Schedules
from openbridge.services.maintenance import Maintenance, periodic
from openbridge.services.provisioning import Provisioning
from openbridge.services.conversations import Conversations
from openbridge.services.triggers import BackgroundProducer
from openbridge.services.router import JobRouter


@dataclass
class Components:
    configuration: object = field(repr=False)
    store: object
    tasks: object
    jobs: object
    context: object
    meter: object
    rate: object
    backend: object
    runtime: object
    conversations: object
    commands: object
    gateway: object
    personas: object
    schedules: object
    maintenance: object
    provisioning: object
    approvals: object
    delivery: object
    resources: object = field(repr=False)


def create_application(configuration=None, *, backend=None, channel_call=None, log=None):
    configuration = configuration or load_config(Paths.discover(Path(__file__).resolve().parents[1]))
    home, config = configuration.paths.home, configuration.data
    if not config.get("owner_ids"):
        raise ConfigurationError("Explicit owner_ids are required before opening runtime state")
    try:
        if any(isinstance(owner, bool) or int(owner) < 1 for owner in config["owner_ids"]):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise ConfigurationError("owner_ids must contain valid user IDs") from None
    try:
        raw_days = (config.get("retention") or {}).get("days", 30)
        if isinstance(raw_days, bool):
            raise ValueError
        retention_days = int(raw_days)
        if retention_days != raw_days:
            raise ValueError
    except (ValueError, TypeError, OverflowError, AttributeError):
        raise ConfigurationError("retention.days must be an integer number of days") from None
    if not 1 <= retention_days <= 365:
        raise ConfigurationError("retention.days must be between 1 and 365")
    for bot in config.get("bots", []):
        prefix = str(bot.get("token", "")).split(":", 1)[0]
        if prefix.isdigit():
            bot["bot_id"] = prefix  # Credential account wins over stale cached metadata.
    log = log or FileLogger(home / "bridge.log")
    transport = JsonHttpClient()
    if backend is None:
        url, auth = load_registration(configuration.paths.service)
        backend = OpenCodeClient(url, auth, transport)
    call = channel_call or TelegramClient(transport).call
    with ExitStack() as resources:
        store = Store(home / "groupbot.db")
        resources.callback(store.close)
        tasks = TaskQueue(home / "groupbot.db")
        resources.callback(tasks.close)
        jobs = JobStore(home / "groupbot.db")
        resources.callback(jobs.close)
        context = ChatBuffer(maxlen=40, snapshot_path=str(home / "konteks.json"))
        context.load()
        budget = config.get("budget") or {}
        meter = UsageMeter(daily_call_cap=budget.get("max_call_per_hari", 80),
                           daily_cost_cap=budget.get("max_cost_hari", 2.0), path=str(home / "usage.json"))
        rate = RateLimiter(max_per_menit=(config.get("rate_limiter") or {}).get("max_per_menit", 6), path=str(home / "rate.json"))
        settings = configuration.workers
        workers = {name: ConversationDispatcher(max_workers=number, max_pending=settings.max_pending,
                    max_per_conversation=settings.per_conversation) for name, number in (
                    ("interactive", settings.interactive), ("background", settings.background))}
        # Lazy callbacks break construction cycles; no callbacks run until the
        # fully assembled application explicitly starts.
        runtime = DurableRuntime(jobs, workers["interactive"], lambda job, payload: router.execute(job, payload),
                                 lambda item: delivery.deliver(item), log, background_dispatcher=workers["background"])
        def accounts():
            return [bot for bot in config.get("bots", []) if isinstance(bot.get("token"), str)
                    and not bot["token"].startswith("ISI") and len(bot["token"]) > 20]
        def resolve(account_id):
            return next((bot for bot in config.get("bots", []) if bot.get("token") and bot_id(bot) == account_id), None)
        delivery = TelegramDelivery(call, resolve, store.enqueue, log, home=home)
        output = OutputService(runtime, delivery)
        def send(bot, chat_id, text, buttons=None, cols=2):
            return output.send(bot_id(bot), chat_id, text, buttons, cols)
        def edit(bot, chat_id, message_id, text, buttons=None, cols=2, parse=False):
            return output.edit(bot_id(bot), chat_id, message_id, text, buttons, cols, parse)
        stopped = threading.Event()
        personas = Personas(home, store, config, log)
        schedules = Schedules(home, store, config, accounts, log)
        maintenance = Maintenance(home, store, call, log)
        approvals = ApprovalRelay(lambda path, method, body: backend.request(path, method, body),
                                  lambda bot, owner, key, request: notify_approval(call, bot, owner, key, request), config["owner_ids"])
        def checkpoint(sid):
            runtime.checkpoint(sid)
            job = runtime.current_job()
            if job and job["status_message_id"]:
                bot = resolve(job["account_id"])
                if bot:
                    call(bot["token"], "editMessageText", {"chat_id": job["conversation_id"],
                        "message_id": job["status_message_id"], "text": f"⚙️ Job #{job['id']} is processing.",
                        "reply_markup": {"inline_keyboard": [[{"text": "Stop", "callback_data": f"st-{sid}"}]]}}, timeout=5)
        conversations = Conversations(home, config, backend, runtime, personas, approvals, context, meter, rate, send, checkpoint, log,
                                      memories=lambda identity, chat: store.list_memories(identity, chat))
        provisioning = Provisioning(home, config, accounts, personas, call, send, edit,
                                     lambda bot: app.start_poller(bot), log)
        authorize = lambda user: authorized(config, user)
        commands = TelegramCommands(config=config, backend=backend, conversations=conversations, personas=personas,
            schedules=schedules, store=store, tasks=tasks, jobs=jobs, approvals=approvals, provision=provisioning,
            authorize=authorize, send=send, edit=edit, call=call, log=log)
        def controls(bot, chat_id, text):
            JobCommands(jobs, backend, lambda text, **options: send(bot, chat_id, text, **options),
                        channel="telegram").handle(bot_id(bot), chat_id, text)
        gateway = TelegramGateway(config=config, call=call, jobs=jobs, store=store, runtime=runtime, context=context,
            conversations=conversations, commands=commands, media=TelegramMedia(home, call, log), schedules=schedules,
            authorize=authorize, send=send, controls=controls, stopped=stopped, log=log)
        router = JobRouter(resolve, config, store, jobs, conversations, gateway, send, call, output)
        producer = BackgroundProducer(jobs, store, matches)
        def schedule_tick():
            schedules.sync()
            personas.sync()
            maintenance.backup()
            producer.schedules_due({bot_id(bot) for bot in accounts()}, time.time())
        def prepare():
            maintenance.secure_credentials()
            if not accounts():
                log("No usable bot token configured")
                return False
            schedules.sync()
            personas.sync()
            maintenance.migrate_chats()
            jobs.recover_legacy_tasks()
            recovered = jobs.recover()
            if recovered:
                log("Interrupted jobs require review:", len(recovered))
            return True
        def retention_tick():
            pruned = jobs.prune_terminal(older_than=retention_days * 86400)
            if pruned:
                log("Retention pruned", len(pruned), "settled jobs")
        loops = {
            "schedule-producer": lambda: periodic(stopped, 20, schedule_tick, log, "Schedule producer"),
            "approval-relay": lambda: periodic(stopped, 2, approvals.poll, log, "Approval relay"),
            "initiative-producer": lambda: periodic(stopped, max(60, int((config.get("trigger") or {}).get("initiatif_detik", 300))),
                lambda: jobs.enqueue_initiatives({bot_id(bot) for bot in accounts()}), log, "Initiative producer", True),
            "task-producer": lambda: periodic(stopped, 5, lambda: producer.tasks_due({bot_id(bot) for bot in accounts()}), log, "Task producer", True),
            "provisioning": lambda: periodic(stopped, 20, provisioning.scan, log, "Provisioning"),
            "legacy-outbox": lambda: periodic(stopped, 3, maintenance.legacy_outbox, log, "Legacy outbox"),
            "retention": lambda: periodic(stopped, 6 * 3600, retention_tick, log, "Retention", True)}
        closer = resources.pop_all()
        app = Application(shutdown=stopped, runtime=runtime, worker_pools=workers, context=context,
            bots=accounts, poller=gateway.poll, account_key=lambda bot: ("telegram", bot["token"].split(":", 1)[0]), prepare=prepare,
            background_loops=loops, log=log, pid_path=home / "bridge.pid", cleanup=closer.close)
        app.components = Components(configuration, store, tasks, jobs, context, meter, rate, backend, runtime,
            conversations, commands, gateway, personas, schedules, maintenance, provisioning, approvals, delivery, closer)
        return app
