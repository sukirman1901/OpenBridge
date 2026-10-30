#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
python3 - "$HERE" <<'PY'
import os, signal, sys
from pathlib import Path
root = Path(sys.argv[1])
pidfile = root / 'bridge.pid'
if not pidfile.exists():
    raise SystemExit('Tidak ada PID bridge tercatat.')
pid = int(pidfile.read_text())
try:
    args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
except FileNotFoundError:
    raise SystemExit('Bridge sudah berhenti.')
if str(root / 'bridge.py').encode() not in args:
    raise SystemExit('PID bukan bridge ini; tidak dihentikan.')
os.kill(pid, signal.SIGTERM)
print('Bridge dihentikan.')
PY
