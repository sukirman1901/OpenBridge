"""Execute background work through injected session/model/delivery boundaries."""
from openbridge.domain.errors import TaskFailed
import re


class BackgroundExecutor:
    def __init__(self, jobs, session, model, emit):
        self.jobs = jobs
        self.session = session
        self.model = model
        self.emit = emit

    def execute(self, job, payload):
        sid = self.session(job)
        if job["kind"] == "schedule":
            self.emit(self.model(sid, payload["prompt"], 120))
        elif job["kind"] == "initiative":
            answer = self.model(sid,
                "Review this conversation. Speak briefly only if useful; otherwise answer exactly SILENT.\n"
                + payload["context"], 120)
            if answer.strip().upper() not in ("SILENT", "DIAM"):
                self.emit(answer)
        elif job["kind"] == "task":
            self.task(job, payload, sid)
        else:
            raise ValueError("Unknown background job kind")

    def task(self, job, payload, sid):
        maximum = int(payload["max_steps"])
        if not 1 <= maximum <= 100:
            raise TaskFailed("Invalid task step budget")
        completed = self.jobs.completed_steps(job["id"])
        if [row["step"] for row in completed] != list(range(len(completed))):
            raise TaskFailed("Task checkpoints are not contiguous")
        if completed and self._report(payload, completed[-1]["answer"]):
            return
        for step in range(len(completed), maximum):
            final = step == maximum - 1
            prompt = (f"[TASK #{payload['task_id']}] Goal: {payload['goal']}\n"
                f"Step {step + 1}/{maximum}. "
                + ("Verify the work with real evidence and write the final REPORT. " if final else
                   "Make a short plan if needed, then perform the next useful action with your tools. ")
                + "Put deliverables under hasil/. Do not claim completion without evidence. "
                  "Start a final report with DONE only if complete, or FAILED if impossible.")
            answer = self.model(sid, prompt, payload.get("wait_seconds", 240))
            self.jobs.record_step(job["id"], step, answer)
            if self._report(payload, answer):
                return
        self.emit(f"⚠️ Task #{payload['task_id']} reached its {maximum}-step limit without a DONE report.")
        raise TaskFailed("Task step budget exhausted")

    def _report(self, payload, answer):
        match = re.match(r"\s*(?:\*\*|__)?(DONE|FAILED|SELESAI|GAGAL)\b", answer, re.I)
        marker = match.group(1).upper() if match else ""
        if marker in ("DONE", "SELESAI"):
            self.emit(f"📋 Task #{payload['task_id']} done\n{answer}")
            return True
        if marker in ("FAILED", "GAGAL"):
            self.emit(f"⚠️ Task #{payload['task_id']} failed\n{answer}")
            raise TaskFailed("The agent reported failure")
        return False
