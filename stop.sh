#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
python3 - "$HERE" <<'PY'
import os, signal, sys
from pathlib import Path
root = Path(sys.argv[1])
pidfile = root / 'bridge.pid'
if not pidfile.exists():
    raise SystemExit('No bridge PID recorded.')
pid = int(pidfile.read_text())
try:
    args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
except FileNotFoundError:
    raise SystemExit('The bridge is already stopped.')
if str(root / 'bridge.py').encode() not in args:
    raise SystemExit('PID does not belong to this bridge; not stopping it.')
os.kill(pid, signal.SIGTERM)
print('Bridge stopped.')
PY
