#!/usr/bin/env bash
# Auto-restart supervision for OpenBridge on hosts without an init system
# (e.g. Termux/proot containers or bare shells).
#
# Semantics (systemd "Restart=on-failure" equivalent):
# - abnormal exit  -> restart with exponential backoff, capped at 5 minutes;
#   the backoff resets after the bridge stayed up for 30 minutes;
# - clean exit (e.g. ./stop.sh ran, which handles SIGTERM gracefully) ->
#   supervision stops. Nothing restarts a bridge you deliberately stopped.
# The single-poller lock inside start.sh keeps this loop and manual launches
# from ever running two pollers on the same token.
set -u
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$HERE"
delay=5
log() { echo "$(date '+%m-%d %H:%M:%S') supervisor: $*" >> bridge.out; }
log "supervision started"
while true; do
    started=$(date +%s)
    ./start.sh
    code=$?
    ran=$(( $(date +%s) - started ))
    if [ "$ran" -ge 1800 ]; then
        delay=5
    fi
    if [ "$code" -eq 0 ]; then
        log "bridge exited cleanly after ${ran}s; supervision stops"
        exit 0
    fi
    log "bridge exited with $code after ${ran}s; restarting in ${delay}s"
    sleep "$delay"
    delay=$(( delay * 2 ))
    if [ "$delay" -gt 300 ]; then
        delay=300
    fi
done
