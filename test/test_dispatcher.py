"""Deterministic queue tests: no network, models, or persisted user state."""
import threading
import unittest

from openbridge.domain.messages import ConversationKey
from openbridge.runtime.dispatcher import ConversationDispatcher, QueueFull


class TestConversationDispatcher(unittest.TestCase):
    def dispatcher(self, **kwargs):
        dispatcher = ConversationDispatcher(**kwargs)
        self.addCleanup(dispatcher.shutdown, 3)
        return dispatcher

    def test_same_conversation_is_fifo_and_never_overlaps(self):
        dispatcher = self.dispatcher(max_workers=3)
        key = ConversationKey("telegram", "account", "chat")
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        order = []

        def first():
            order.append("first-start")
            started.set()
            self.assertTrue(release.wait(2))
            order.append("first-end")

        a = dispatcher.submit(key, first)
        self.assertTrue(started.wait(2))
        b = dispatcher.submit(key, lambda: order.append("second"))
        self.assertEqual(order, ["first-start"])
        release.set()
        a.result(2)
        b.result(2)
        self.assertEqual(order, ["first-start", "first-end", "second"])

    def test_other_conversations_do_not_wait_for_a_busy_conversation(self):
        dispatcher = self.dispatcher(max_workers=2)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocked():
            started.set()
            return release.wait(2)

        a = dispatcher.submit("a", blocked)
        self.assertTrue(started.wait(2))
        a2 = dispatcher.submit("a", lambda: "later")
        b = dispatcher.submit("b", lambda: "independent")
        self.assertEqual(b.result(2), "independent")
        self.assertFalse(a2.done())
        release.set()
        self.assertTrue(a.result(2))
        self.assertEqual(a2.result(2), "later")

    def test_failed_work_does_not_block_the_next_turn(self):
        dispatcher = self.dispatcher()

        def fail():
            raise ValueError("failed")

        first = dispatcher.submit("a", fail)
        second = dispatcher.submit("a", lambda: 42)
        with self.assertRaises(ValueError):
            first.result(2)
        self.assertEqual(second.result(2), 42)

    def test_cancelled_work_does_not_block_the_next_turn(self):
        dispatcher = self.dispatcher(max_workers=1)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocked():
            started.set()
            release.wait(2)

        first = dispatcher.submit("a", blocked)
        self.assertTrue(started.wait(2))
        cancelled = dispatcher.submit("a", lambda: self.fail("Cancelled work ran"))
        self.assertTrue(cancelled.cancel())
        last = dispatcher.submit("a", lambda: "ok")
        release.set()
        first.result(2)
        self.assertEqual(last.result(2), "ok")

    def test_limits_include_running_work(self):
        dispatcher = self.dispatcher(max_pending=2, max_per_conversation=1)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocked():
            started.set()
            release.wait(2)

        dispatcher.submit("a", blocked)
        self.assertTrue(started.wait(2))
        with self.assertRaises(QueueFull):
            dispatcher.submit("a", lambda: None)
        dispatcher.submit("b", blocked)
        with self.assertRaises(QueueFull):
            dispatcher.submit("c", lambda: None)
        release.set()

    def test_shutdown_drains_work_and_rejects_new_submissions(self):
        dispatcher = self.dispatcher()
        futures = [dispatcher.submit("a", lambda n=n: n) for n in range(8)]
        self.assertTrue(dispatcher.shutdown(2))
        self.assertEqual([f.result() for f in futures], list(range(8)))
        with self.assertRaises(RuntimeError):
            dispatcher.submit("a", lambda: None)

    def test_shutdown_reports_deadline_without_replaying_work(self):
        dispatcher = self.dispatcher()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocked():
            started.set()
            release.wait(2)

        future = dispatcher.submit("a", blocked)
        self.assertTrue(started.wait(2))
        self.assertFalse(dispatcher.shutdown(0))
        release.set()
        future.result(2)
        self.assertTrue(dispatcher.shutdown(2))

    def test_idle_keys_are_released_and_worker_count_is_bounded(self):
        dispatcher = self.dispatcher(max_workers=2)
        for n in range(100):
            self.assertEqual(dispatcher.submit(n, lambda n=n: n).result(2), n)
        self.assertTrue(dispatcher.shutdown(2))
        self.assertEqual(dispatcher._queues, {})
        self.assertEqual(len(dispatcher._threads), 2)

    def test_identity_separates_channels_and_accounts(self):
        keys = {
            ConversationKey("telegram", "1", "42"),
            ConversationKey("telegram", "2", "42"),
            ConversationKey("whatsapp", "1", "42"),
        }
        self.assertEqual(len(keys), 3)
        with self.assertRaises(ValueError):
            ConversationKey("", "1", "42")

    def test_invalid_limits_are_rejected(self):
        for values in ({"max_workers": 0}, {"max_pending": 0}, {"max_per_conversation": 0}):
            with self.assertRaises(ValueError):
                ConversationDispatcher(**values)
