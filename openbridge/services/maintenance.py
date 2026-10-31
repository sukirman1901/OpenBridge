"""Backups, legacy migration, credential modes, and bounded periodic loops."""
from datetime import datetime, timedelta, timezone
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import time

from openbridge.infrastructure.files import read_json


def periodic(stopped, interval, action, log, name, initial_wait=False):
    if initial_wait and stopped.wait(interval):
        return
    while not stopped.is_set():
        try:
            action()
        except Exception as error:
            log(name, "failed:", type(error).__name__)
        if stopped.wait(interval):
            return


class Maintenance:
    def __init__(self, home, store, call, log):
        self.home, self.store, self.call, self.log = Path(home), store, call, log
        self.last_backup = None

    def secure_credentials(self):
        paths = [*self.home.glob("*.token"), self.home / "pool.json", self.home / "provision.json"]
        for path in paths:
            if path.is_file():
                path.chmod(0o600)

    def migrate_chats(self):
        path = self.home / "chats.json"
        try:
            data = read_json(path, {})
            if not isinstance(data, dict) or not data:
                return
            for identity, title in data.items():
                self.store.save_chat(identity, title)
            path.rename(str(path) + ".imported")
        except (OSError, ValueError, TypeError):
            return

    def backup(self):
        day = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d")
        if self.last_backup == day:
            return
        directory = self.home / "backup"
        directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", suffix=".db", dir=directory)
        os.close(fd)
        temporary = Path(temporary)
        archive_tmp = directory / (temporary.name + ".tar.gz")
        try:
            with closing(sqlite3.connect(self.home / "groupbot.db")) as source, closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
            with tarfile.open(archive_tmp, "w:gz") as archive:
                archive.add(temporary, arcname="backup/snapshot.db")
                for name in ("config.json", "personas.json", "cron.json", "usage.json", "konteks.json", "rate.json", "provision.json"):
                    path = self.home / name
                    if path.is_file():
                        archive.add(path, arcname=name)
                memory = self.home / "memori"
                if memory.is_dir():
                    archive.add(memory, arcname="memori")
            archive_tmp.chmod(0o600)
            destination = directory / f"gb-{day}.tar.gz"
            os.replace(archive_tmp, destination)
            self.last_backup = day
            for old in sorted(directory.glob("gb-????-??-??.tar.gz"))[:-7]:
                old.unlink()
            for path in (self.home / "media").glob("*"):
                if path.is_file() and time.time() - path.stat().st_mtime > 7 * 86400:
                    path.unlink()
            self.log("Daily backup saved", destination.name)
        finally:
            temporary.unlink(missing_ok=True)
            archive_tmp.unlink(missing_ok=True)

    def legacy_outbox(self):
        for item in self.store.due_outbox():
            result = self.call(item["token"], "sendMessage", {"chat_id": item["chat_id"], "text": item["text"]})
            if result.get("ok"):
                self.store.mark_sent(item["id"])
            elif item["attempts"] >= 6:
                self.store.mark_dead(item["id"])
