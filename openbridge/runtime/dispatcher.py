"""Bounded FIFO execution per key, with concurrency across independent keys.

Keys may include a lane and a conversation identity. This scheduler is in
memory and never retries work itself. DurableRuntime owns SQLite acceptance,
recovery, and separate worker-pool reservations for chat/background lanes.
"""
from collections import deque
from concurrent.futures import Future
import threading
import time


class QueueFull(RuntimeError):
    pass


class ConversationDispatcher:
    def __init__(self, max_workers=2, max_pending=128, max_per_conversation=32):
        if min(max_workers, max_pending, max_per_conversation) < 1:
            raise ValueError("Worker and queue limits must be positive")
        self.max_workers = max_workers
        self.max_pending = max_pending
        self.max_per_conversation = max_per_conversation
        self._condition = threading.Condition()
        self._queues = {}
        self._ready = deque()
        self._active = set()
        self._pending = 0
        self._threads = []
        self._closed = False

    def submit(self, key, function, *args, **kwargs):
        with self._condition:
            if self._closed:
                raise RuntimeError("Conversation dispatcher is closed")
            count = len(self._queues.get(key, ())) + (key in self._active)
            if self._pending >= self.max_pending or count >= self.max_per_conversation:
                raise QueueFull("Conversation queue is full")
            if not self._threads:
                self._threads = [threading.Thread(
                    target=self._work, name=f"conversation-worker-{index}", daemon=True)
                    for index in range(self.max_workers)]
                for thread in self._threads:
                    thread.start()
            future = Future()
            queue = self._queues.setdefault(key, deque())
            queue.append((future, function, args, kwargs))
            self._pending += 1
            if key not in self._active and len(queue) == 1:
                self._ready.append(key)
            self._condition.notify_all()
            return future

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._ready or (self._closed and not self._pending))
                if not self._ready:
                    return
                key = self._ready.popleft()
                future, function, args, kwargs = self._queues[key].popleft()
                self._active.add(key)
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        result = function(*args, **kwargs)
                    except BaseException as error:
                        future.set_exception(error)
                    else:
                        future.set_result(result)
            finally:
                with self._condition:
                    self._active.remove(key)
                    self._pending -= 1
                    if self._queues[key]:
                        self._ready.append(key)
                    else:
                        del self._queues[key]
                    self._condition.notify_all()

    def shutdown(self, timeout=None):
        """Reject new work, drain accepted work, return whether workers exited."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            threads = list(self._threads)
        for thread in threads:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            thread.join(remaining)
        return all(not thread.is_alive() for thread in threads)
