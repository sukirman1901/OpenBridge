#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$HERE"
# Foreground process; use a process manager for durable supervision.
# The lock prevents two Telegram pollers consuming the same token's updates.
exec flock --nonblock --no-fork "$HERE/bridge.lock" python3 -u "$HERE/bridge.py"
