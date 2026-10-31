"""Injected application lifecycle. Importing it does not load state or start work."""
import os
from pathlib import Path
import signal
import threading
import time


class Application:
    def __init__(self, *, shutdown, runtime, worker_pools, context, bots, poller, account_key,
                 prepare, background_loops, log, pid_path=None,
                 monitor_seconds=30, drain_seconds=20, cleanup=None):
        self.shutdown = shutdown
        self.runtime = runtime
        self.worker_pools = worker_pools
        self.context = context
        self.bots = bots
        self.poller = poller
        self.account_key = account_key
        self.prepare = prepare
        self.background_loops = background_loops
        self.log = log
        self.pid_path = pid_path
        self.monitor_seconds = monitor_seconds
        self.drain_seconds = drain_seconds
        self.cleanup = cleanup
        self.closed = False
        self.pollers = {}
        self.loop_threads = []
        self.lock = threading.RLock()

    def _poll(self, bot, index):
        try:
            self.poller(bot, index)
        except Exception as error:
            self.log("Channel poller stopped:", type(error).__name__)

    def start_poller(self, bot, index=0):
        # An injected, non-secret account key prevents the provisioner and
        # supervision loop from starting duplicate pollers for one account.
        account_id = self.account_key(bot)
        with self.lock:
            if self.shutdown.is_set():
                return None
            previous = self.pollers.get(account_id)
            if previous and previous.is_alive():
                return previous
            thread = threading.Thread(target=self._poll, args=(bot, index),
                                      name=f"channel-poller-{index}", daemon=True)
            self.pollers[account_id] = thread
            thread.start()
            return thread

    def _background(self, name, target):
        try:
            target()
        except Exception as error:
            self.log("Background loop stopped:", name, type(error).__name__)

    def _signals(self):
        if threading.current_thread() is not threading.main_thread():
            return {}
        previous = {}

        def stop(signum, frame):
            self.log("Shutdown signal received")
            self.shutdown.set()

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, stop)
        return previous

    def _stop(self):
        if self.closed:
            return
        self.shutdown.set()
        runtime_stopped = True
        try:
            self.runtime.stop()
        except Exception as error:
            runtime_stopped = False
            self.log("Runtime stop failed:", type(error).__name__)
        deadline = time.monotonic() + self.drain_seconds
        for pool in self.worker_pools.values():
            try:
                pool.shutdown(timeout=0)
            except Exception as error:
                self.log("Worker stop failed:", type(error).__name__)
        drained = True
        for pool in self.worker_pools.values():
            try:
                drained = pool.shutdown(timeout=max(0, deadline - time.monotonic())) and drained
            except Exception as error:
                self.log("Worker drain failed:", type(error).__name__)
                drained = False
        with self.lock:
            threads = [*self.loop_threads, *self.pollers.values()]
        runtime_threads = getattr(self.runtime, "threads", [])
        if isinstance(runtime_threads, (list, tuple)):
            threads.extend(runtime_threads)
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            self.log("Auxiliary threads still stopping; storage connections remain open")
        try:
            self.context.save()
        except Exception as error:
            self.log("Context snapshot failed:", type(error).__name__)
        if not drained:
            self.log("Shutdown deadline reached; running jobs require reconciliation on restart")
        self.closed = runtime_stopped and drained and not any(thread.is_alive() for thread in threads)
        if self.closed and self.cleanup:
            self.cleanup()
        self.log("OpenBridge stopped")

    def close(self):
        self._stop()

    def run(self):
        previous_signals = self._signals()
        try:
            if not self.prepare() or self.shutdown.is_set():
                return
            if self.pid_path:
                Path(self.pid_path).write_text(str(os.getpid()))
            self.runtime.start()
            for name, target in self.background_loops.items():
                thread = threading.Thread(target=self._background, args=(name, target), name=name, daemon=True)
                self.loop_threads.append(thread)
                thread.start()
            for index, bot in enumerate(list(self.bots())):
                self.start_poller(bot, index)
            self.log("Execution workers:", {name: pool.max_workers for name, pool in self.worker_pools.items()})
            self.log("OpenBridge running with", len(self.bots()), "accounts")
            while not self.shutdown.wait(self.monitor_seconds):
                for index, bot in enumerate(list(self.bots())):
                    self.start_poller(bot, index)
        finally:
            try:
                self._stop()
            finally:
                for signum, handler in previous_signals.items():
                    signal.signal(signum, handler)


def main():
    from openbridge.bootstrap import create_application
    create_application().run()
