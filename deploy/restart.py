#!/usr/bin/env python3
"""Drain accepted execution before restarting this bridge, never kill active work."""
import argparse
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time


def running_jobs(home):
    with sqlite3.connect(f'file:{home / "groupbot.db"}?mode=ro', uri=True) as db:
        executions = db.execute("SELECT COUNT(*) FROM runtime_jobs WHERE status='running'").fetchone()[0]
        deliveries = db.execute("SELECT COUNT(*) FROM runtime_deliveries WHERE status='sending'").fetchone()[0]
        return executions + deliveries


def owned_process(home, pid):
    try:
        return str(home / 'bridge.py').encode() in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    except FileNotFoundError:
        return False


def restart(home, timeout=1200):
    marker = home / 'bridge.drain'
    # Exclusive creation: never remove another operator's drain request.
    fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        pid = int((home / 'bridge.pid').read_text())
        deadline = time.monotonic() + timeout
        if not owned_process(home, pid):
            raise RuntimeError('Recorded PID is not this bridge')
        while running_jobs(home):
            if time.monotonic() >= deadline:
                raise TimeoutError('Drain timed out; bridge was NOT stopped')
            time.sleep(.5)
        os.kill(pid, signal.SIGTERM)
        while owned_process(home, pid):
            if time.monotonic() >= deadline:
                raise TimeoutError('Graceful stop not confirmed; no replacement was started')
            time.sleep(.25)
        # The cleanly exiting supervisor stops itself. The single-poller lock
        # protects the handover even if its shell has not exited yet.
        marker.unlink()
        with (home / 'bridge.out').open('ab') as output:
            subprocess.Popen(['bash', str(home / 'supervise.sh')], cwd=home,
                             stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                             start_new_session=True)
    finally:
        marker.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--timeout', type=int, default=1200)
    args = parser.parse_args()
    restart(args.home.resolve(), args.timeout)
