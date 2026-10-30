#!/usr/bin/env python3
"""
Pembatas laju panggilan model per menit, per bot (jendela geser 60 dtk).
- Sliding window: {bot_id: [ts, ts, ...]} — timestamp time.time() dalam 60 dtk terakhir
- allow() all-or-nothing: batch n yang lewat batas ditolak tanpa mencatat apa pun
- max_per_menit None = tanpa batas (selalu True, tetap mencatat untuk statistik)
- Persist JSON atomik (tmp → os.replace), tahan file rusak, thread-safe
Import: from rate_limiter import RateLimiter
"""
import json
import os
import threading
import time

JENDELA = 60.0  # detik — ukuran jendela geser per bot


def _kunci(bot_id):
    """Kunci bot selalu string, apa pun yang dikirim caller."""
    return str(bot_id)


class RateLimiter:
    """Pembatas laju per bot: {bot_id: [ts, ...]} — lock + persist."""

    def __init__(self, max_per_menit=None, path=None):
        self.max_per_menit = max_per_menit
        self.path = path
        self.lock = threading.Lock()
        self._riwayat = {}  # {bot_id: [ts, ts, ...]}
        if path:
            self._load()

    # ── internal ─────────────────────────────────────────────────
    def _prune(self, antrian, kini):
        """Buang timestamp yang sudah keluar dari jendela 60 dtk (in-place)."""
        antrian[:] = [ts for ts in antrian if kini - ts < JENDELA]
        return antrian

    def _antrian(self, bot_id, kini):
        """Antrian bot yang sudah dipangkas ke jendela aktif."""
        return self._prune(self._riwayat.setdefault(_kunci(bot_id), []), kini)

    # ── persist ──────────────────────────────────────────────────
    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            if isinstance(d, dict):
                kini = time.time()
                bersih = {}
                for bot, antrian in d.items():
                    # sanitasi: bentuk aneh dibuang diam-diam
                    if isinstance(bot, str) and isinstance(antrian, list):
                        ts_oke = []
                        for ts in antrian:
                            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                                ts_oke.append(float(ts))
                        bersih[bot] = self._prune(ts_oke, kini)
                self._riwayat = bersih
        except FileNotFoundError:
            self._riwayat = {}
        except Exception:
            self._riwayat = {}  # file rusak → mulai bersih, jangan bikin bridge mati

    def _save(self):
        """Tulis atomik (tmp → replace) supaya crash tidak meninggalkan file setengah."""
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            # simpan versi terpangkas — file tidak menumpuk timestamp basi
            kini = time.time()
            bersih = {b: self._prune(list(tr), kini)
                      for b, tr in self._riwayat.items() if tr}
            with open(tmp, "w") as f:
                json.dump(bersih, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass  # disk penuh/dilarang — behavior: jalan terus, limiter sisi-memori

    # ── API ──────────────────────────────────────────────────────
    def allow(self, bot_id, n=1):
        """True kalau n panggilan masih muat di jendela 60 dtk bot itu.
        SIDE-EFFECT saat diizinkan: catat n timestamp baru (all-or-nothing).
        Kalau ditolak → False, riwayat tidak disentuh. max None = selalu True."""
        n = max(1, int(n))
        kini = time.time()
        with self.lock:
            antrian = self._antrian(bot_id, kini)
            if self.max_per_menit is not None and len(antrian) + n > self.max_per_menit:
                return False  # ditolak: tidak mencatat apa pun
            antrian.extend([kini] * n)
            self._save()
            return True

    def used_last_minute(self, bot_id):
        """Jumlah panggilan bot itu dalam 60 detik terakhir (entri lama ikut dibersihkan)."""
        kini = time.time()
        with self.lock:
            return len(self._antrian(bot_id, kini))

    def reset(self, bot_id=None):
        """Hapus riwayat: satu bot, atau semua kalau bot_id None."""
        with self.lock:
            if bot_id is None:
                self._riwayat = {}
            else:
                self._riwayat.pop(_kunci(bot_id), None)
            self._save()
