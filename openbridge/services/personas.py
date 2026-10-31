"""Persona inbox/mirror and private memory compatibility, without agent execution."""
from datetime import datetime
import json
from pathlib import Path

from openbridge.infrastructure.files import read_json, write_json, safe_name


class Personas:
    def __init__(self, home, store, config, log):
        self.home = Path(home)
        self.store = store
        self.config = config
        self.log = log
        self.path = self.home / "personas.json"

    def list(self):
        rows = self.store.list_personas()
        if rows:
            return [{"nama": row["nama"], "persona": row["persona"]} for row in rows]
        first = (self.config.get("bots") or [{}])[0]
        return [{"nama": first.get("nama", "Bot"), "persona": first.get("persona", "")}]

    def archive(self, name, description):
        path = self.home / "backup" / "personas.arsip.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                handle.write(json.dumps({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                         "nama": name, "persona": description}) + "\n")
            path.chmod(0o600)
            return True
        except OSError as error:
            self.log("Persona archive failed", type(error).__name__)
            return False

    def add(self, name, description):
        name = safe_name(name)
        if any(row["nama"] == name for row in self.store.list_personas()):
            return
        self.store.upsert_persona(name, description)
        try:
            source = read_json(self.path, None)
        except (OSError, ValueError):
            return  # Keep an unreadable owner file intact for review.
        if source is None:
            source = [{"nama": row["nama"], "persona": row["persona"]} for row in self.store.list_personas()]
        elif isinstance(source, list) and not any(isinstance(row, dict) and row.get("nama") == name for row in source):
            source.append({"nama": name, "persona": description})
        if isinstance(source, list):
            write_json(self.path, source)

    def sync(self):
        exists = self.path.exists()
        try:
            source = read_json(self.path, [])
            if not isinstance(source, list):
                return
            if any(not isinstance(item, dict) or not item.get("nama") or not item.get("persona") for item in source):
                return  # Invalid input must never cause deletion of valid records.
            for item in source:
                safe_name(item["nama"])
        except (OSError, ValueError, TypeError):
            return
        if exists:
            for item in source:
                self.store.upsert_persona(item["nama"].strip(), str(item["persona"]))
            wanted = {item["nama"].strip() for item in source}
            for item in self.store.list_personas():
                if item["nama"] not in wanted and self.archive(item["nama"], item["persona"]):
                    self.store.delete_persona(item["nama"])
        mirror = [{"nama": row["nama"], "persona": row["persona"]} for row in self.store.list_personas()]
        if not exists or source != mirror:
            write_json(self.path, mirror)

    def memory(self, name):
        name = safe_name(name)
        path = self.home / "memori" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            try:
                with path.open("x") as handle:
                    handle.write(f"# Private memory of {name}\n(written by you, for you)\n")
                path.chmod(0o600)
            except FileExistsError:
                pass
        return path
