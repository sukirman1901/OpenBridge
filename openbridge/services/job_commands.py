"""Scoped owner job controls, independent of Telegram and native backend paths.

The channel must authorize the sender before invoking this trusted controller.
The bound send callback targets only that account/conversation.
"""
import json

from openbridge.ports.execution import ExecutionBackend


class JobCommands:
    commands = frozenset(("/job", "/jobs", "/resolve", "/result", "/cancel"))

    def __init__(self, jobs, backend: ExecutionBackend, send, *, channel):
        self.jobs = jobs
        self.backend = backend
        self.send = send
        self.channel = channel

    def handle(self, account_id, conversation_id, text):
        parts = text.split()
        command = parts[0].split("@")[0] if parts else ""
        if command not in self.commands:
            self.send("Unknown job control. Use /jobs, /job <id>, /result <id>, or /resolve <id>.")
            return
        if command == "/jobs" or (command == "/job" and len(parts) == 1):
            jobs = self.jobs.list_jobs(account_id, conversation_id, channel=self.channel)
            report = "\n".join(f"#{job['id']} ({job['kind']}/{job['lane']}): {job['status']}; "
                               f"delivery: {job['delivery_state'] or 'none'}" for job in jobs) or "No durable jobs yet."
            self.send(report + "\nUse /result <id> to retrieve a saved result, /cancel <id> to cancel a queued one.")
            return
        if len(parts) < 2 or not parts[1].isdigit():
            self.send("Usage: /job <id>, /result <id>, /cancel <id>, or /resolve <id> confirm")
            return
        job = self.jobs.get(int(parts[1]))
        if (not job or job["channel"] != self.channel or job["account_id"] != account_id
                or job["conversation_id"] != str(conversation_id)):
            self.send("Job not found in this conversation.")
            return
        if command == "/cancel":
            if job["status"] == "queued":
                if self.jobs.cancel_queued(job["id"]):
                    self.send(f"🚫 Job #{job['id']} cancelled; it never started running and stays examinable in /jobs.")
                else:
                    self.send(f"Job #{job['id']} already running; it can no longer be cancelled here.")
                return
            self.send(f"Job #{job['id']} is already running; it can no longer be cancelled here."
                      if job["status"] == "running" else f"Job #{job['id']} is {job['status']}; nothing to cancel.")
            return
        if command == "/job":
            actions = [("Stop", f"st-{job['session_id']}")] if job["status"] == "running" and job["session_id"] else None
            if job["status"] == "queued":
                actions = [("Cancel", f"jx-{job['id']}")]
            progress = ""
            if job["kind"] == "task":
                task = json.loads(job["payload"])
                progress = f"\nTask #{task['task_id']}: {len(self.jobs.completed_steps(job['id']))}/{task['max_steps']} steps checkpointed."
            self.send(f"Job #{job['id']} ({job['kind']}): {job['status']}\n"
                      f"Use /result {job['id']} for saved output.{progress}", buttons=actions)
            return
        if command == "/result":
            outputs = json.loads(job["result"] or "[]")
            if not outputs and job["kind"] == "task":
                steps = self.jobs.completed_steps(job["id"])
                if steps:
                    self.send("Latest checkpointed step (not a final result):\n" + steps[-1]["answer"])
                    return
            self.send("\n\n".join(output["text"] for output in outputs)
                      or f"Job #{job['id']}: {job['status']}; no saved text result.")
            return
        if job["status"] != "interrupted":
            self.send("Only interrupted jobs need reconciliation.")
            return
        if len(parts) != 3 or parts[2] != "confirm":
            self.send(f"Review job #{job['id']} first (session: {job['session_id'] or 'not checkpointed'}). "
                      "Confirm that its actions are accounted for, then use /resolve "
                      f"{job['id']} confirm. This cancels the interrupted attempt; it does not retry it.")
            return
        if job["session_id"] and not self.backend.session_idle(job["session_id"]):
            self.send("Cannot verify an idle backend session; review it before reconciliation.")
            return
        self.jobs.resolve(job["id"])
        self.send(f"Job #{job['id']} reconciled without replay. Queued jobs can continue.")
