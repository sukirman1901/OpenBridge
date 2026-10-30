#!/usr/bin/env python3
"""
Per-bot usage/cost meter — the daily budget guard (financial).
- Accumulated per local Asia/Jakarta day (constant UTC+7, no tzdata)
- Safe extraction from OpenCode session/message objects (missing fields → 0)
- JSON persistence per day, resilient to corrupt files, thread-safe
Import: from usage_meter import UsageMeter
"""
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta

JAKARTA = timezone(timedelta(hours=7))  # WIB, constant offset — no tzdata needed


def local_day(ts=None):
    """'YYYY-MM-DD' date according to Jakarta local time (constant UTC+7)."""
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts, JAKARTA).strftime("%Y-%m-%d")


def _num(x):
    """Safe number: bool/int/float accepted, anything else → 0.0."""
    if isinstance(x, bool):
        return float(x)
    if isinstance(x, (int, float)):
        return float(x)
    return 0.0


def _collect_tokens(v):
    """From the 'tokens' field: may be an int, a per-type dict, or junk → safe total."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if not isinstance(v, dict):
        return 0.0
    total = 0.0
    for k in ("input", "output", "reasoning"):
        total += _num(v.get(k))
    cache = v.get("cache") or {}
    if isinstance(cache, (int, float)) and not isinstance(cache, bool):
        total += float(cache)
    elif isinstance(cache, dict):
        for k in ("read", "write", "cached"):
            total += _num(cache.get(k))
    return total


def _extract(obj):
    """Extract (cost, tokens) from an OpenCode session/message dict; missing/broken → 0."""
    if not isinstance(obj, dict):
        return 0.0, 0.0
    cost = _num(obj.get("cost"))
    tokens = _collect_tokens(obj.get("tokens"))
    # wrapper objects, e.g. {"data": {...}} — peek inside as well
    d = obj.get("data")
    if isinstance(d, dict):
        c2, t2 = _extract(d)
        cost += c2
        tokens += t2
    return cost, tokens


class UsageMeter:
    """Per-bot meter: {day: {bot_id: {calls, cost, tokens}}} — lock + persistence."""

    def __init__(self, daily_call_cap=None, daily_cost_cap=None, path=None):
        self.daily_call_cap = daily_call_cap
        self.daily_cost_cap = daily_cost_cap
        self.path = path
        self.lock = threading.Lock()
        self._data = {}   # {day: {bot_id: {"calls": int, "cost": float, "tokens": float}}}
        if path:
            self._load()

    # ── persist ──────────────────────────────────────────────────
    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            if isinstance(d, dict):
                # sanitize: silently discard malformed shapes
                clean = {}
                for day, bots in d.items():
                    if isinstance(day, str) and isinstance(bots, dict):
                        entry = {}
                        for b, v in bots.items():
                            if isinstance(v, dict):
                                entry[str(b)] = {
                                    "calls": int(_num(v.get("calls"))),
                                    "cost": _num(v.get("cost")),
                                    "tokens": _num(v.get("tokens")),
                                }
                        clean[day] = entry
                self._data = clean
        except FileNotFoundError:
            self._data = {}
        except Exception:
            self._data = {}  # corrupt file → start clean, never take the bridge down

    def _save(self):
        """Write atomically (tmp → replace) so a crash never leaves a half-written file."""
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass  # disk full/forbidden — behavior: keep running, in-memory meter only

    # ── API ──────────────────────────────────────────────────────
    def record(self, bot_id, calls=1, cost=0.0, tokens=0):
        """Accumulate a bot's usage into the local (WIB) day."""
        day = local_day()
        with self.lock:
            entry = self._data.setdefault(day, {}).setdefault(str(bot_id),
                                                              {"calls": 0, "cost": 0.0, "tokens": 0.0})
            entry["calls"] = int(_num(entry.get("calls"))) + int(_num(calls))
            entry["cost"] = _num(entry.get("cost")) + _num(cost)
            entry["tokens"] = _num(entry.get("tokens")) + _num(tokens)
            self._save()

    def extract_and_record(self, bot_id, obj):
        """From an OpenCode session/message dict: extract cost & tokens, record 1 call."""
        cost, tokens = _extract(obj)
        self.record(bot_id, calls=1, cost=cost, tokens=tokens)
        return cost, tokens

    def today(self, bot_id):
        """Today's (WIB) usage summary for a single bot."""
        day = local_day()
        with self.lock:
            v = (self._data.get(day) or {}).get(str(bot_id)) or {}
            return {"calls": int(_num(v.get("calls"))),
                    "cost": _num(v.get("cost")),
                    "tokens": int(_num(v.get("tokens")))}

    def over_budget(self, bot_id, calls=0, cost=0.0):
        """True when the candidate addition would exceed today's caps (None = ∞).
        Landing exactly AT the limit is still allowed — only more than that is rejected."""
        day = local_day()
        with self.lock:
            v = (self._data.get(day) or {}).get(str(bot_id)) or {}
            call_ada = int(_num(v.get("calls")))
            cost_ada = _num(v.get("cost"))
            if self.daily_call_cap is not None and call_ada + int(_num(calls)) > self.daily_call_cap:
                return True
            if self.daily_cost_cap is not None and cost_ada + _num(cost) > self.daily_cost_cap:
                return True
            return False
