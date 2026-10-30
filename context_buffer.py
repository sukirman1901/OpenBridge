#!/usr/bin/env python3
"""
ContextBuffer — per-chat conversation context buffer (ring).
The bot can "see" older messages it is no longer replying to: each chat
holds a bounded deque with maxlen, plus a JSON snapshot so it survives restarts.
"""
import json
import os
import threading
import time
from collections import deque
from datetime import datetime

ROLE_VALID = ("user", "bot")
CLIP = 120  # length limit for each context line's text


def _jam(ts):
    """Turn ts (epoch or string) into 'HH:MM'. On failure → '??:??'."""
    if ts is None:
        return datetime.now().strftime("%H:%M")
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts).strftime("%H:%M")
        except (OSError, OverflowError, ValueError):
            return "??:??"
    teks = str(ts).strip()
    # already HH:MM (possibly with seconds) → use as-is
    for kandidat in (teks, teks[:19]):  # try the full string, then an ISO-style slice
        try:
            if len(kandidat) == 5:
                return datetime.strptime(kandidat, "%H:%M").strftime("%H:%M")
            return datetime.strptime(kandidat, "%H:%M:%S").strftime("%H:%M")
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(kandidat).strftime("%H:%M")
        except ValueError:
            pass
    # "YYYY-MM-DD HH:MM[:SS]" common style
    try:
        return datetime.strptime(teks, "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
    except ValueError:
        pass
    try:
        return datetime.strptime(teks, "%Y-%m-%d %H:%M").strftime("%H:%M")
    except ValueError:
        pass
    return "??:??"


class ChatBuffer:
    """Per-chat_id ring buffer of messages, thread-safe, saveable to JSON."""

    def __init__(self, maxlen=40, snapshot_path=None):
        self.maxlen = max(1, int(maxlen))
        self.snapshot_path = snapshot_path
        self._buf = {}  # chat_id(str) → deque of dict
        self._lock = threading.Lock()

    # ── core ─────────────────────────────────────────────────────
    def append(self, chat_id, role, text, name=None, ts=None):
        """Append a message to the chat buffer; role restricted to {'user','bot'}."""
        if role not in ROLE_VALID:
            return False
        kunci = str(chat_id)
        entri = {
            "role": role,
            "text": str(text or ""),
            "name": str(name) if name is not None else None,
            "ts": float(ts) if isinstance(ts, (int, float)) else (str(ts) if ts else time.time()),
        }
        with self._lock:
            dq = self._buf.setdefault(kunci, deque(maxlen=self.maxlen))
            dq.append(entri)  # the deque automatically drops the oldest entry when full
        return True

    def recent(self, chat_id, n=None):
        """The last n messages (default all), ordered oldest → newest."""
        with self._lock:
            dq = self._buf.get(str(chat_id))
            items = list(dq) if dq else []
        if n is not None:
            items = items[-max(0, int(n)):]
        return [dict(e) for e in items]  # copies, so the caller can modify them safely

    def format_context(self, chat_id, n=None):
        """String ready to send to the model: '[HH:MM] name: text' per line. Empty → ''. """
        baris = []
        for e in self.recent(chat_id, n):
            teks = str(e.get("text") or "")
            if len(teks) > CLIP:
                teks = teks[:CLIP] + "…"
            nama = e.get("name") or e.get("role") or "?"
            baris.append(f"[{_jam(e.get('ts'))}] {nama}: {teks}")
        return "\n".join(baris)

    # ── JSON snapshot ────────────────────────────────────────────
    def save(self):
        """Save all buffers to snapshot_path (JSON per chat). On failure → stay silent."""
        if not self.snapshot_path:
            return False
        try:
            with self._lock:
                data = {k: list(v) for k, v in self._buf.items()}
            folder = os.path.dirname(os.path.abspath(self.snapshot_path))
            os.makedirs(folder, exist_ok=True)
            sementara = self.snapshot_path + ".tmp"
            with open(sementara, "w") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(sementara, self.snapshot_path)
            return True
        except OSError:
            pass
        except Exception:
            pass
        return False

    def load(self):
        """Load a snapshot; corrupt/anomalous file → empty without raising an exception."""
        if not self.snapshot_path or not os.path.exists(self.snapshot_path):
            return False
        try:
            with open(self.snapshot_path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return False
            baru = {}
            for kunci, entri in data.items():
                if not isinstance(entri, list):
                    continue
                bersih = [
                    e for e in entri
                    if isinstance(e, dict)
                    and e.get("role") in ROLE_VALID
                    and isinstance(e.get("text", ""), str)
                ]
                if bersih:
                    baru[str(kunci)] = deque(bersih[-self.maxlen:], maxlen=self.maxlen)
            with self._lock:
                self._buf = baru
            return True
        except Exception:
            with self._lock:
                self._buf = {}
            return False
