#!/usr/bin/env python3
"""
ContextBuffer — buffer konteks pembicaraan per-chat (ring).
Bot bisa "melihat" pesan lama yang tidak lagi menjawabnya: tiap chat
punya deque terbatas maxlen, snapshot JSON agar tahan restart.
"""
import json
import os
import threading
import time
from collections import deque
from datetime import datetime

ROLE_VALID = ("user", "bot")
POTONG_TEKS = 120  # batas panjang teks per baris konteks


def _jam(ts):
    """Ubah ts (epoch atau string) jadi 'HH:MM'. Gagal → '??:??'."""
    if ts is None:
        return datetime.now().strftime("%H:%M")
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts).strftime("%H:%M")
        except (OSError, OverflowError, ValueError):
            return "??:??"
    teks = str(ts).strip()
    # sudah berupa HH:MM (mungkin dengan detik) → pakai langsung
    for kandidat in (teks, teks[:19]):  # coba utuh, lalu potongan ala ISO
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
    # "YYYY-MM-DD HH:MM[:SS]" gaya umum
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
    """Ring buffer pesan per chat_id, thread-safe, bisa disimpan ke JSON."""

    def __init__(self, maxlen=40, snapshot_path=None):
        self.maxlen = max(1, int(maxlen))
        self.snapshot_path = snapshot_path
        self._buf = {}  # chat_id(str) → deque of dict
        self._lock = threading.Lock()

    # ── inti ─────────────────────────────────────────────────────
    def append(self, chat_id, role, text, name=None, ts=None):
        """Tambah pesan ke buffer chat; role dibatasi {'user','bot'}."""
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
            dq.append(entri)  # deque otomatis buang paling tua saat penuh
        return True

    def recent(self, chat_id, n=None):
        """n pesan terakhir (default semua), urut paling lama → paling baru."""
        with self._lock:
            dq = self._buf.get(str(chat_id))
            items = list(dq) if dq else []
        if n is not None:
            items = items[-max(0, int(n)):]
        return [dict(e) for e in items]  # salinan agar aman dimodifikasi pemanggil

    def format_context(self, chat_id, n=None):
        """String siap-kirim ke model: '[HH:MM] nama: teks' per baris. Kosong → ''. """
        baris = []
        for e in self.recent(chat_id, n):
            teks = str(e.get("text") or "")
            if len(teks) > POTONG_TEKS:
                teks = teks[:POTONG_TEKS] + "…"
            nama = e.get("name") or e.get("role") or "?"
            baris.append(f"[{_jam(e.get('ts'))}] {nama}: {teks}")
        return "\n".join(baris)

    # ── snapshot JSON ────────────────────────────────────────────
    def save(self):
        """Simpan semua buffer ke snapshot_path (JSON per chat). Gagal → diam."""
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
        """Muat snapshot; file rusak/anomali → kosong tanpa exception."""
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
