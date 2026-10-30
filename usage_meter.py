#!/usr/bin/env python3
"""
Meteran penggunaan/biaya per bot — pengagard budget harian (finansi).
- Akumulasi per hari lokal Asia/Jakarta (UTC+7 konstan, tanpa tzdata)
- Extract aman dari objek sesi/message OpenCode (field hilang → 0)
- Persist JSON per hari, tahan file rusak, thread-safe
Import: from usage_meter import UsageMeter
"""
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta

JAKARTA = timezone(timedelta(hours=7))  # WIB, offset konstan — no tzdata needed


def lokal_hari(ts=None):
    """Tanggal 'YYYY-MM-DD' versi jam lokal Jakarta (UTC+7 konstan)."""
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts, JAKARTA).strftime("%Y-%m-%d")


def _num(x):
    """Angka aman: bool/int/float diterima, sisanya 0.0."""
    if isinstance(x, bool):
        return float(x)
    if isinstance(x, (int, float)):
        return float(x)
    return 0.0


def _tarik_tokens(v):
    """Dari field 'tokens': bisa int, dict per jenis, atau sampah → total aman."""
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


def _ekstrak(obj):
    """Tarik (cost, tokens) dari dict sesi/message OpenCode; hilang/rusak → 0."""
    if not isinstance(obj, dict):
        return 0.0, 0.0
    cost = _num(obj.get("cost"))
    tokens = _tarik_tokens(obj.get("tokens"))
    # objek pembungkus, mis. {"data": {...}} — intip di dalam juga
    d = obj.get("data")
    if isinstance(d, dict):
        c2, t2 = _ekstrak(d)
        cost += c2
        tokens += t2
    return cost, tokens


class UsageMeter:
    """Meteran per bot: {hari: {bot_id: {calls, cost, tokens}}} — lock + persist."""

    def __init__(self, daily_call_cap=None, daily_cost_cap=None, path=None):
        self.daily_call_cap = daily_call_cap
        self.daily_cost_cap = daily_cost_cap
        self.path = path
        self.lock = threading.Lock()
        self._data = {}   # {hari: {bot_id: {"calls": int, "cost": float, "tokens": float}}}
        if path:
            self._load()

    # ── persist ──────────────────────────────────────────────────
    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            if isinstance(d, dict):
                # sanitasi: bentuk aneh dibuang diam-diam
                bersih = {}
                for hari, bots in d.items():
                    if isinstance(hari, str) and isinstance(bots, dict):
                        entri = {}
                        for b, v in bots.items():
                            if isinstance(v, dict):
                                entri[str(b)] = {
                                    "calls": int(_num(v.get("calls"))),
                                    "cost": _num(v.get("cost")),
                                    "tokens": _num(v.get("tokens")),
                                }
                        bersih[hari] = entri
                self._data = bersih
        except FileNotFoundError:
            self._data = {}
        except Exception:
            self._data = {}  # file rusak → mulai bersih, jangan bikin bridge mati

    def _save(self):
        """Tulis atomik (tmp → replace) supaya crash tidak meninggalkan file setengah."""
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass  # disk penuh/dilarang — behavior: jalan terus, meteran sisi-memori

    # ── API ──────────────────────────────────────────────────────
    def record(self, bot_id, calls=1, cost=0.0, tokens=0):
        """Akumulasi pemakaian bot ke hari lokal (WIB)."""
        hari = lokal_hari()
        with self.lock:
            entri = self._data.setdefault(hari, {}).setdefault(str(bot_id),
                                                              {"calls": 0, "cost": 0.0, "tokens": 0.0})
            entri["calls"] = int(_num(entri.get("calls"))) + int(_num(calls))
            entri["cost"] = _num(entri.get("cost")) + _num(cost)
            entri["tokens"] = _num(entri.get("tokens")) + _num(tokens)
            self._save()

    def extract_and_record(self, bot_id, obj):
        """Dari dict sesi/message OpenCode: tarik cost & tokens, catat 1 panggilan."""
        cost, tokens = _ekstrak(obj)
        self.record(bot_id, calls=1, cost=cost, tokens=tokens)
        return cost, tokens

    def today(self, bot_id):
        """Ringkasan pemakaian hari ini (WIB) untuk satu bot."""
        hari = lokal_hari()
        with self.lock:
            v = (self._data.get(hari) or {}).get(str(bot_id)) or {}
            return {"calls": int(_num(v.get("calls"))),
                    "cost": _num(v.get("cost")),
                    "tokens": int(_num(v.get("tokens")))}

    def over_budget(self, bot_id, calls=0, cost=0.0):
        """True kalau penambahan kandidat melewati cap hari ini (None = ∞).
        TepAT DI BATAS masih boleh — hanya lebih yang ditolak."""
        hari = lokal_hari()
        with self.lock:
            v = (self._data.get(hari) or {}).get(str(bot_id)) or {}
            call_ada = int(_num(v.get("calls")))
            cost_ada = _num(v.get("cost"))
            if self.daily_call_cap is not None and call_ada + int(_num(calls)) > self.daily_call_cap:
                return True
            if self.daily_cost_cap is not None and cost_ada + _num(cost) > self.daily_cost_cap:
                return True
            return False
