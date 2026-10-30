#!/usr/bin/env python3
"""test_task_queue.py — unittest for task_queue.py (autonomous task queue)."""
import os
import sqlite3
import tempfile
import unittest

from task_queue import TaskQueue, iso_from_ts


class TestTaskQueue(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.path)          # let SQLite create the file itself
        self.list_db = []             # tracks temp files for cleanup

    def tearDown(self):
        for p in self.list_db:
            for suf in ("", "-wal", "-shm"):
                if os.path.exists(p + suf):
                    os.remove(p + suf)

    def buat_file_db(self):
        self.list_db.append(self.path)
        return self.path

    # 1) add + get ------------------------------------------------
    def test_add_then_get(self):
        tq = TaskQueue()  # :memory:
        tid = tq.add_task("botA", 111, "fetch a news summary", max_steps=3)
        t = tq.get(tid)
        self.assertIsNotNone(t)
        self.assertEqual(t["bot_id"], "botA")
        self.assertEqual(t["chat_id"], 111)
        self.assertEqual(t["goal"], "fetch a news summary")
        self.assertEqual(t["status"], "queued")
        self.assertEqual(t["steps"], 0)
        self.assertEqual(t["max_steps"], 3)
        self.assertIsNone(t["result"])
        self.assertIsNone(t["last_error"])
        # time columns filled in ISO format (UTC+7)
        self.assertEqual(t["created_at"], t["updated_at"])
        self.assertEqual(t["created_at"], iso_from_ts())  # ~now
        self.assertIsNone(tq.get(9999))
        tq.close()

    # 2) claim_next: queue order & only 'queued' -------------------
    def test_claim_next_order_and_only_running(self):
        tq = TaskQueue()
        a = tq.add_task("botA", 1, "task-A")
        b = tq.add_task("botA", 1, "task-B")
        # the first task becomes 'running' via claim — must not be claimed again
        claim1 = tq.claim_next("botA")
        self.assertEqual(claim1["id"], a)
        self.assertEqual(claim1["status"], "running")
        # recorded as 'running' in the DB as well
        self.assertEqual(tq.get(a)["status"], "running")
        # next claim: task-B (the oldest still 'queued')
        claim2 = tq.claim_next("botA")
        self.assertEqual(claim2["id"], b)
        self.assertEqual(claim2["status"], "running")
        # exhausted — queue is empty
        self.assertIsNone(tq.claim_next("botA"))
        # a task already 'failed' is not claimed
        c = tq.add_task("botA", 1, "task-C")
        tq.fail(c, "yahoo error")
        self.assertIsNone(tq.claim_next("botA"))
        tq.close()

    def test_claim_next_isolasi_per_bot(self):
        tq = TaskQueue()
        tq.add_task("botA", 1, "belongs to A")
        self.assertIsNone(tq.claim_next("botB"))          # never crosses bots
        klaim = tq.claim_next("botA")
        self.assertEqual(klaim["goal"], "belongs to A")
        # partial table: two TaskQueue instances on the same file, table stays one
        # (verified locally via :memory: — separate instances stay separate; this one is a file)
        tq.close()

    # 3) max_steps: ok, ok, full → final --------------------------
    def test_bump_habiskan_langkah_gagal(self):
        tq = TaskQueue()
        tid = tq.add_task("botA", 1, "long cycle", max_steps=3)
        tq.claim_next("botA")
        self.assertEqual(tq.bump(tid), "ok")              # 1 < 3
        self.assertEqual(tq.bump(tid), "ok")              # 2 < 3
        self.assertEqual(tq.get(tid)["steps"], 2)
        self.assertEqual(tq.get(tid)["status"], "running")
        self.assertEqual(tq.bump(tid), "full")           # 3 == max → done-for
        t = tq.get(tid)
        self.assertEqual(t["status"], "failed")
        self.assertEqual(t["last_error"], "exceeded max_steps")
        self.assertEqual(t["steps"], 3)
        # after failure, extra bumps are stable: 'full', status unchanged
        self.assertEqual(tq.bump(tid), "full")
        self.assertEqual(tq.get(tid)["status"], "failed")
        self.assertEqual(tq.get(tid)["steps"], 3)
        # bump a task that does not exist
        self.assertEqual(tq.bump(424242), "hilang")
        tq.close()

    # 4) complete/fail idempotent ---------------------------
    def test_selesaikan_gagalkan_idempoten(self):
        tq = TaskQueue()
        tid = tq.add_task("botA", 1, "task to finish")
        tq.claim_next("botA")
        self.assertTrue(tq.complete(tid, "recap delivered"))
        t = tq.get(tid)
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["result"], "recap delivered")
        # call again: idempotent, result & status unchanged
        self.assertFalse(tq.complete(tid, "recap delivered again"))
        t = tq.get(tid)
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["result"], "recap delivered")
        # fail on top of 'done': idempotent too, result untouched
        self.assertFalse(tq.fail(tid, "xxx"))
        self.assertEqual(tq.get(tid)["status"], "done")
        self.assertIsNone(tq.get(tid)["last_error"])

        tid2 = tq.add_task("botA", 1, "task to fail")
        self.assertTrue(tq.fail(tid2, "verification failed"))
        self.assertEqual(tq.get(tid2)["status"], "failed")
        self.assertEqual(tq.get(tid2)["last_error"], "verification failed")
        self.assertFalse(tq.fail(tid2, "oh well"))
        self.assertEqual(tq.get(tid2)["last_error"], "verification failed")
        self.assertFalse(tq.complete(tid2, "no"))
        self.assertEqual(tq.get(tid2)["status"], "failed")
        tq.close()

    # 5) list_tasks: filters & ordering ---------------------------
    def test_list_tasks_filter_and_order(self):
        tq = TaskQueue()
        tq.add_task("botA", 1, "A1")
        tq.add_task("botA", 1, "A2")
        tq.add_task("botB", 2, "B1")
        tq.add_task("botA", 3, "A3")
        # ordered newest first
        semua = tq.list_tasks(limit=50)
        self.assertEqual([t["goal"] for t in semua], ["A3", "B1", "A2", "A1"])
        # filter by bot
        self.assertEqual([t["goal"] for t in tq.list_tasks(bot_id="botA")],
                         ["A3", "A2", "A1"])
        # filter by status
        b1 = tq.claim_next("botB")
        self.assertEqual([t["goal"] for t in tq.list_tasks(status="running")],
                         ["B1"])
        tq.complete(b1["id"], "done")
        self.assertEqual([t["goal"] for t in tq.list_tasks(status="done")],
                         ["B1"])
        # filters combined: newest first, botA only
        self.assertEqual([t["goal"] for t in tq.list_tasks(bot_id="botA", limit=2)],
                         ["A3", "A2"])
        tq.close()

    # 6) count ----------------------------------------------------
    def test_count(self):
        tq = TaskQueue()
        self.assertEqual(tq.count(), 0)
        a = tq.add_task("botA", 1, "X1")
        tq.add_task("botA", 1, "X2")
        tq.add_task("botB", 2, "Y1")
        self.assertEqual(tq.count(), 3)
        self.assertEqual(tq.count(bot_id="botA"), 2)
        self.assertEqual(tq.count(status="queued"), 3)
        tq.claim_next("botA")
        self.assertEqual(tq.count(status="queued"), 2)
        self.assertEqual(tq.count(status="running"), 1)
        tq.complete(a, "ok")
        self.assertEqual(tq.count(status="done"), 1)
        self.assertEqual(tq.count(status="queued", bot_id="botB"), 1)
        self.assertEqual(tq.count(status="failed"), 0)
        tq.close()

    # 7) persist across file reopen --------------------------------
    def test_persist_reopen_file(self):
        path = self.buat_file_db()
        tq = TaskQueue(path)
        tid = tq.add_task("botA", 1, "survives a restart", max_steps=2)
        b = tq.add_task("botA", 1, "second")
        tq.claim_next("botA")                             # tid → 'running'
        tq.complete(tid, "closed before shutdown")
        tq.fail(999, "nonexistent")                   # no-op
        tq.close()

        tq2 = TaskQueue(path)
        t = tq2.get(tid)
        self.assertIsNotNone(t)
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["result"], "closed before shutdown")
        self.assertEqual(t["goal"], "survives a restart")
        self.assertEqual(t["max_steps"], 2)
        klaim = tq2.claim_next("botA")                    # b is still 'queued'
        self.assertIsNotNone(klaim)
        self.assertEqual(klaim["id"], b)
        # the cycle continues on the new instance too
        self.assertEqual(tq2.bump(b), "ok")
        tq2.close()

    # 8) legacy tables are not broken -------------------------------
    def test_tabel_schedules_tetap_utuh(self):
        path = self.buat_file_db()
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE schedules (id INTEGER PRIMARY KEY, bot_id TEXT,"
            " chat_id INTEGER, expr TEXT, kind TEXT, prompt TEXT,"
            " active INTEGER DEFAULT 1)")
        conn.execute(
            "INSERT INTO schedules(bot_id, chat_id, expr, kind, prompt)"
            " VALUES('lama', 1, '08:30', 'cron', 'old schedule')")
        conn.commit()
        conn.close()

        tq = TaskQueue(path)                              # CREATE TABLE IF NOT EXISTS tasks
        tid = tq.add_task("botA", 1, "new task")
        tq.complete(tid, "matches")
        tq.close()

        conn = sqlite3.connect(path)
        bar = conn.execute(
            "SELECT bot_id, prompt FROM schedules").fetchall()
        conn.close()
        self.assertEqual(bar, [("lama", "old schedule")])

    # 9) full-cycle scenario (goal → plan → act → verify → report)
    def test_siklus_penuh_report(self):
        tq = TaskQueue()
        tid = tq.add_task("botA", 100, "summarize the daily group", max_steps=5)
        task = tq.claim_next("botA")
        self.assertEqual(task["chat_id"], 100)
        for _ in range(4):                                # plan, act, verify x3
            self.assertEqual(tq.bump(tid), "ok")
        self.assertEqual(tq.get(tid)["steps"], 4)
        self.assertTrue(tq.complete(tid, "5-step recap delivered to the chat"))
        t = tq.get(tid)
        self.assertEqual(t["status"], "done")
        self.assertIn("delivered", t["result"])
        self.assertIsNone(t["last_error"])
        self.assertEqual(tq.count(status="done"), 1)
        tq.close()

    # extra: an invalid max_steps can still be inserted, bump goes full immediately
    def test_max_steps_satu_langsung_penuh(self):
        tq = TaskQueue()
        tid = tq.add_task("botA", 1, "one step", max_steps=1)
        tq.claim_next("botA")
        self.assertEqual(tq.bump(tid), "full")
        t = tq.get(tid)
        self.assertEqual(t["status"], "failed")
        self.assertEqual(t["last_error"], "exceeded max_steps")
        tq.close()

    def test_iso_from_ts_utc7(self):
        # 2026-06-12 01:30 UTC → 08:30 WIB
        import calendar
        from datetime import datetime, timedelta
        utc = datetime(2026, 6, 12, 1, 30)
        ts = calendar.timegm(utc.utctimetuple())
        self.assertEqual(iso_from_ts(ts), "2026-06-12 08:30:00")
        self.assertEqual(iso_from_ts(ts + 86400), "2026-06-13 08:30:00")


if __name__ == "__main__":
    unittest.main()
