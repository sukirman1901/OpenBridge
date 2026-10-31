"""Persistent work coordination. Execution and delivery never retry each other."""
import json
import threading

from openbridge.domain.messages import ConversationKey
from openbridge.domain.jobs import LANES
from openbridge.domain.errors import ExecutionUncertain
from openbridge.domain.errors import RetryLater
from openbridge.runtime.dispatcher import QueueFull


class DurableRuntime:
    def __init__(self, store, dispatcher, execute, deliver, log, background_dispatcher=None):
        self.store = store
        self.dispatcher = dispatcher
        self.background_dispatcher = background_dispatcher if background_dispatcher is not None else dispatcher
        self.execute = execute
        self.deliver = deliver
        self.log = log
        self.stopped = threading.Event()
        self.local = threading.local()
        self.scheduled = set()
        self.lock = threading.Lock()
        self.threads = []

    def current_job(self):
        return getattr(self.local, "job", None)

    def emit(self, output):
        if self.current_job() is None:
            return False
        self.local.outputs.append(output)
        return True

    def checkpoint(self, sid):
        job = self.current_job()
        if job:
            self.store.checkpoint(job["id"], sid)
            job["session_id"] = sid

    def run(self, job_id):
        with self.lock:
            if self.stopped.is_set():
                return  # Accepted work remains durable for the next startup.
            job = self.store.claim(job_id)
        if job is None:
            return
        self.local.job = job
        self.local.outputs = []
        status, error = "succeeded", None
        try:
            self.execute(job, json.loads(job["payload"]))
            if not self.local.outputs and job["status_message_id"]:
                self.local.outputs.append({"operation": "send", "text":
                    "✅ Done."})
        except RetryLater as pause:
            self.store.defer(job_id, pause.delay)
            return
        except Exception as exc:
            status = "interrupted" if isinstance(exc, ExecutionUncertain) else "failed"
            error = type(exc).__name__
            self.local.outputs.append({"operation": "send", "text": (
                "⚠️ Processing was interrupted. Please check /jobs before trying again."
                if status == "interrupted" else "⚠️ I couldn't finish that request. Check /jobs for details.")})
            self.log("Job execution failed", job_id, error)
        finally:
            outputs = self.local.outputs
            self.local.job = None
            self.local.outputs = []
        self.store.finish(job_id, outputs, status, error)

    def pump(self):
        # Each lane gets its own query window. A large background backlog cannot
        # hide a newer chat job beyond a shared SQL LIMIT.
        for lane in LANES:
            self._pump_lane(lane)

    def _pump_lane(self, lane):
        dispatcher = self.dispatcher if lane == "interactive" else self.background_dispatcher
        for job in self.store.pending(lane=lane):
            if self.stopped.is_set():
                return
            job_id = job["id"]
            with self.lock:
                if job_id in self.scheduled:
                    continue
                self.scheduled.add(job_id)
            try:
                # Reminders get a distinct in-memory key so a long task in the
                # same chat never delays them; they serialize among themselves.
                tag = "reminder" if job["kind"] == "schedule" else lane
                future = dispatcher.submit((tag, ConversationKey(
                    job["channel"], job["account_id"], job["conversation_id"])), self.run, job_id)
            except QueueFull:
                with self.lock:
                    self.scheduled.discard(job_id)
                continue  # A busy conversation must not starve another one.
            except RuntimeError:
                with self.lock:
                    self.scheduled.discard(job_id)
                return  # All remaining jobs stay durable in SQLite.

            def completed(future, job_id=job_id):
                with self.lock:
                    self.scheduled.discard(job_id)
                if not future.cancelled() and future.exception():
                    self.log("Job persistence/worker failure", job_id, type(future.exception()).__name__)
            future.add_done_callback(completed)

    def deliver_one(self):
        delivery = self.store.claim_delivery()
        if delivery is None:
            return False
        try:
            outcome, error, retry_after = self.deliver(delivery)
        except Exception as exc:
            # A send exception may occur after the destination accepted it.
            outcome = "pending" if delivery["payload"]["operation"] == "edit" else "uncertain"
            error, retry_after = type(exc).__name__, None
        self.store.delivery_result(delivery, outcome, error, retry_after)
        return True

    def start(self):
        for name, action, interval in (("job-dispatch", self.pump, .25),
                                       ("job-delivery", self.deliver_one, .5)):
            def loop(action=action, interval=interval):
                while not self.stopped.is_set():
                    try:
                        action()
                    except Exception as exc:
                        self.log("Runtime loop failure", type(exc).__name__)
                    self.stopped.wait(interval)
            thread = threading.Thread(target=loop, name=name, daemon=True)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        with self.lock:
            self.stopped.set()
        for thread in self.threads:
            thread.join(timeout=2)
