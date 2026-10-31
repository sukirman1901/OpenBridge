"""Owner-confirmed account provisioning. No automatic pool consumption."""
import re
import secrets
import threading

from openbridge.infrastructure.files import read_json, write_json, safe_name


class Provisioning:
    def __init__(self, home, config, accounts, personas, call, send, edit, start_poller, log):
        self.home, self.config, self.accounts, self.personas = home, config, accounts, personas
        self.call, self.send, self.edit, self.start_poller, self.log = call, send, edit, start_poller, log
        self.pending = {}
        self.lock = threading.RLock()

    def scan(self):
        if not self.config.get("provisioning_enabled", False):
            return
        requests = read_json(self.home / "provision.json", [])
        if not isinstance(requests, list):
            return
        for request in requests:
            if isinstance(request, dict) and request.get("nama"):
                try:
                    self.offer(request["nama"])
                except ValueError:
                    self.log("Invalid provisioning name retained for owner review")

    def offer(self, name):
        name = safe_name(str(name)[:30])
        bots = self.accounts()
        if not bots:
            return
        owner = int(self.config["owner_ids"][0])
        with self.lock:
            if any(item[2] == name for item in self.pending.values()):
                return
            key = secrets.token_hex(12)
            self.pending[key] = (owner, bots[0]["token"].split(":", 1)[0], name)
        response = self.send(bots[0], owner, f"👶 Request to create bot '{name}'. Use a configured spare token?",
                  buttons=[("Approve", f"pv-y-{key}"), ("Deny", f"pv-n-{key}")])
        if isinstance(response, dict) and not response.get("ok"):
            with self.lock:
                self.pending.pop(key, None)

    def decide(self, bot, chat_id, message_id, choice, key, user_id):
        with self.lock:
            pending = self.pending.get(key)
            authorized = int(user_id) in {int(owner) for owner in self.config.get("owner_ids", [])}
            if (not authorized or int(chat_id) != int(user_id) or not pending
                    or pending[:2] != (int(user_id), bot["token"].split(":", 1)[0])):
                return False
            name = pending[2]
            del self.pending[key]
            if choice == "y":
                ok, detail = self.create(name)
                text = f"Bot '{name}' created as @{detail}." if ok else f"Bot creation failed: {detail}"
            else:
                text = f"Bot request '{name}' denied."
            source = read_json(self.home / "provision.json", [])
            if isinstance(source, list):
                write_json(self.home / "provision.json", [row for row in source if not isinstance(row, dict) or row.get("nama") != name])
        self.edit(bot, chat_id, message_id, text)
        return True

    def create(self, name):
        name = safe_name(name)
        pool_path = self.home / "pool.json"
        pool = read_json(pool_path, [])
        if not isinstance(pool, list) or not pool or not isinstance(pool[0], dict):
            return False, "no spare token configured"
        token = pool[0].get("token", "")
        identity = self.call(token, "getMe")
        if not identity.get("ok"):
            return False, "token identity not confirmed; pool unchanged"
        if any(bot["token"].split(":", 1)[0] == token.split(":", 1)[0] for bot in self.config.get("bots", [])):
            return False, "account is already configured"
        slug = re.sub(r"[^\w-]+", "-", name.lower()).strip("-")
        if not slug:
            return False, "name cannot be used as a token filename"
        filename = slug + ".token"
        path = self.home / filename
        if path.exists():
            return False, "token file already exists; refusing to overwrite"
        # A crash may leave an unreferenced token file, but cannot overwrite one.
        with path.open("x") as handle:
            handle.write(token + "\n")
        path.chmod(0o600)
        source = read_json(self.home / "config.json", {})
        persona = f"You are {name}, a helpful assistant."
        source.setdefault("bots", []).append({"nama": name, "persona": persona, "token": "{file:" + filename + "}"})
        write_json(self.home / "config.json", source)
        write_json(pool_path, pool[1:])
        bot = {"nama": name, "persona": persona, "token": token}
        self.config.setdefault("bots", []).append(bot)
        self.personas.add(name, persona)
        self.start_poller(bot)
        return True, identity["result"].get("username", "bot")
