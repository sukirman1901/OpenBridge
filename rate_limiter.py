#!/usr/bin/env python3
"""
Per-bot, per-minute rate limiter for model calls (60-second sliding window).
- Sliding window: {bot_id: [ts, ts, ...]} — time.time() timestamps within the last 60s
- allow() is all-or-nothing: a batch that would exceed the limit is rejected
  without recording anything
- max_per_menit None = unlimited (always True, still recorded for statistics)
- Atomic JSON persistence (tmp → os.replace), resilient to corrupt files, thread-safe
Import: from rate_limiter import RateLimiter
"""
import json
import os
import threading
import time

WINDOW = 60.0  # seconds — size of the per-bot sliding window


def _kunci(bot_id):
    """Bot keys are always strings, whatever the caller passes."""
    return str(bot_id)


class RateLimiter:
    """Per-bot rate limiter: {bot_id: [ts, ...]} — lock + persistence."""

    def __init__(self, max_per_menit=None, path=None):
        self.max_per_menit = max_per_menit
        self.path = path
        self.lock = threading.Lock()
        self._riwayat = {}  # {bot_id: [ts, ts, ...]}
        if path:
            self._load()

    # ── internal ─────────────────────────────────────────────────
    def _prune(self, queue, now):
        """Drop timestamps that have left the 60-second window (in place)."""
        queue[:] = [ts for ts in queue if now - ts < WINDOW]
        return queue

    def _queue(self, bot_id, now):
        """The bot's queue, pruned down to the active window."""
        return self._prune(self._riwayat.setdefault(_kunci(bot_id), []), now)

    # ── persist ──────────────────────────────────────────────────
    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            if isinstance(d, dict):
                now = time.time()
                bersih = {}
                for bot, queue in d.items():
                    # sanitize: silently discard malformed shapes
                    if isinstance(bot, str) and isinstance(queue, list):
                        ts_oke = []
                        for ts in queue:
                            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                                ts_oke.append(float(ts))
                        bersih[bot] = self._prune(ts_oke, now)
                self._riwayat = bersih
        except FileNotFoundError:
            self._riwayat = {}
        except Exception:
            self._riwayat = {}  # corrupt file → start clean, never take the bridge down

    def _save(self):
        """Write atomically (tmp → replace) so a crash never leaves a half-written file."""
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            # persist the pruned version — stale timestamps never pile up in the file
            now = time.time()
            bersih = {b: self._prune(list(tr), now)
                      for b, tr in self._riwayat.items() if tr}
            with open(tmp, "w") as f:
                json.dump(bersih, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass  # disk full/forbidden — behavior: keep running, in-memory limiter only

    # ── API ──────────────────────────────────────────────────────
    def allow(self, bot_id, n=1):
        """True when n calls still fit in that bot's 60-second window.
        SIDE-EFFECT when allowed: record n fresh timestamps (all-or-nothing).
        When rejected → False, history is untouched. max None = always True."""
        n = max(1, int(n))
        now = time.time()
        with self.lock:
            queue = self._queue(bot_id, now)
            if self.max_per_menit is not None and len(queue) + n > self.max_per_menit:
                return False  # rejected: nothing is recorded
            queue.extend([now] * n)
            self._save()
            return True

    def used_last_minute(self, bot_id):
        """Number of calls that bot made in the last 60 seconds (stale entries pruned too)."""
        now = time.time()
        with self.lock:
            return len(self._queue(bot_id, now))

    def reset(self, bot_id=None):
        """Clear history: one bot, or everything when bot_id is None."""
        with self.lock:
            if bot_id is None:
                self._riwayat = {}
            else:
                self._riwayat.pop(_kunci(bot_id), None)
            self._save()
