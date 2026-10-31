"""Pure due-time evaluation and small durable background producers."""
from datetime import datetime, timezone
import math


def schedule_occurrence(row, now, cron_matches):
    """Return (deduplication key, legacy-compatible firing mark), or None.

    Cron matching receives an epoch (the legacy matcher uses UTC+7). Stored
    once/interval timestamps keep their original host-local interpretation.
    Missed cron minutes are not backfilled; overdue one-shots are accepted once.
    """
    if not row["active"]:
        return None
    current = datetime.fromtimestamp(now)
    kind, last = row["kind"], row["last_fired"]
    if kind == "cron":
        try:
            due = cron_matches(row["expr"], now)
        except ValueError:
            return None
        mark = current.strftime("%Y-%m-%d %H:%M")
        return (f"{row['id']}:minute:{int(now // 60)}", mark) if due and last != mark else None
    if kind == "once":
        try:
            # Stored one-shot expressions are UTC-naive (gmtime convention);
            # forever trust that, regardless of the host's timezone. Reading
            # them as local time makes reminders fire seven hours early on
            # TZ=Asia/Jakarta hosts.
            due = not last and datetime.fromisoformat(row["expr"]).replace(
                tzinfo=timezone.utc).timestamp() <= now
        except ValueError:
            return None
        return (f"{row['id']}:once", current.strftime("%Y-%m-%d %H:%M:%S")) if due else None
    if kind == "interval":
        expression = row["expr"].strip().lower()
        try:
            units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
            seconds = float(expression[:-1]) * units[expression[-1]] if expression[-1:] in units else float(expression)
            if not math.isfinite(seconds) or seconds < 1:
                return None
            previous = datetime.fromisoformat(last).timestamp() if last else None
        except (ValueError, IndexError):
            return None
        if previous is None or now - previous >= seconds:
            return f"{row['id']}:interval:{last or 'first'}", current.strftime("%Y-%m-%d %H:%M:%S")
    return None


class BackgroundProducer:
    def __init__(self, jobs, schedules, cron_matches):
        self.jobs = jobs
        self.schedules = schedules
        self.cron_matches = cron_matches

    def schedules_due(self, account_ids, now):
        created = []
        for row in self.schedules.list_schedules(active_only=True):
            if str(row["bot_id"]) not in account_ids:
                continue
            occurrence = schedule_occurrence(row, now, self.cron_matches)
            if occurrence:
                job_id = self.jobs.enqueue_schedule(row, *occurrence)
                if job_id is not None:
                    created.append(job_id)
        return created

    def tasks_due(self, account_ids):
        return [self.jobs.enqueue_task(task_id, account_id) for account_id in account_ids
                for task_id in self.jobs.task_candidates(account_id)]
