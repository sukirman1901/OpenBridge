#!/usr/bin/env python3
"""OpenBridge first-run wizard.

Interactive (or fully flag-driven) setup that:
  1. collects bot name, Telegram bot token and owner id,
  2. writes a mode-0600 token file + a safe `config.json`
     (token via the `{file:...}` indirection — never inline),
  3. checks the OpenCode server (`opencode serve`) is reachable,
  4. offers to boot the bridge.

Run with no flags for prompts, or e.g.:
    python3 wizard.py --name MyBot --token 123:ABC --owner-id 12345 --yes
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.environ.get("OPENBRIDGE_SERVICE_JSON") or \
    os.path.expanduser("~/.local/state/opencode/service.json")


def check_python():
    if sys.version_info < (3, 8):
        sys.exit("❌ Python 3.8+ is required.")
    return True


def check_opencode(server_wait=False):
    """Verify OpenCode server credentials exist; guide the user when not."""
    if os.path.exists(SERVICE):
        try:
            svc = json.load(open(SERVICE))
            url, pw = svc["url"], svc["password"]
            print(f"✅ OpenCode server found: {url}")
            return True
        except (ValueError, KeyError):
            print("⚠️  service.json is corrupt — re-run `opencode serve`.")
            return False
    print("⚠️  OpenCode server not detected.\n"
          "   In another terminal run:\n"
          "     1) curl -fsSL https://opencode.ai/install | bash   (once)\n"
          "     2) opencode auth login                             (pick provider)\n"
          "     3) opencode serve --port 4096                      (keep running)")
    if server_wait:
        for _ in range(60):
            time.sleep(5)
            if os.path.exists(SERVICE):
                return check_opencode()
    return False


def build_config(name, token_file, owner_id):
    """Return the initial config dict (schema keys match bridge.py's reads)."""
    return {
        "bots": [{
            "token": "{file:%s}" % token_file,
            "nama": name,
            "persona": ("You are a friendly, quick-witted group companion. "
                        "Helpful and concise: at most 3 short sentences per reply."),
        }],
        "owner_ids": [int(owner_id)],
        "budget": {"max_call_per_hari": 80, "max_cost_hari": 2.0},
        "trigger": {"cooldown_detik": 15, "max_char_reply": 1500,
                    "initiatif_detik": 300},
        "rate_limiter": {"max_per_menit": 6},
    }


def ask(prompt, default="", validator=None):
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            raw = default
        if validator and not validator(raw):
            print("   ✖ invalid, try again")
            continue
        return raw


def main():
    ap = argparse.ArgumentParser(description="OpenBridge setup wizard")
    ap.add_argument("--name", help="bot display name")
    ap.add_argument("--token", help="Telegram bot token from @BotFather")
    ap.add_argument("--owner-id", help="your Telegram user id (owner)")
    ap.add_argument("--yes", action="store_true", help="non-interactive mode")
    ap.add_argument("--wait-server", action="store_true",
                    help="wait up to 5 min for `opencode serve`")
    args = ap.parse_args()

    check_python()
    print("🌉 OpenBridge setup — 3 quick questions then you're live.\n")

    inter = not (args.name and args.token and args.owner_id)
    name = args.name or ask("Bot name (shown in Telegram)", "MyBot")
    token = args.token or ask("Bot token (from @BotFather)")
    owner = args.owner_id or ask("Your Telegram user id (owner)")

    if ":" not in token:
        print("   ⚠️  that does not look like a BotFather token (usually 123456:AAAA…)"
              " — keeping it anyway, the bridge will fail fast if it is wrong.")
    if not owner.isdigit():
        sys.exit("✖ owner id must be a number (get it from @userinfobot)")

    token_file = f"{name.lower().replace(' ', '-')}.token"
    token_path = os.path.join(HERE, token_file)

    write_it = True
    if os.path.exists(token_path):
        write_it = args.yes or input(
            f"⚠️  {token_file} exists — overwrite? [y/N] ").lower() == "y"
        if not write_it:
            print("   kept the existing token file")
    if write_it:
        with open(token_path, "w") as fh:
            fh.write(token.strip())
        os.chmod(token_path, 0o600)
        print(f"✅ token saved to {token_file} (mode 600)")

    config_path = os.path.join(HERE, "config.json")
    if os.path.exists(config_path):
        if args.yes or input("⚠️  config.json exists — overwrite? [y/N] ").lower() != "y":
            print("   kept the existing config.json — done!")
            return
    with open(config_path, "w") as fh:
        json.dump(build_config(name, token_file, owner), fh, indent=2)
    print("✅ config.json written (token via {file:...} indirection)")

    if check_opencode(server_wait=args.wait_server):
        if args.yes or input("\n▶️  Start the bridge now? [Y/n] ").strip().lower() in ("", "y"):
            os.system(os.path.join(HERE, "start.sh"))
    else:
        print("\n🏁 Config is ready. Once `opencode serve` is running, start with:\n"
              f"   cd {HERE} && ./start.sh")


if __name__ == "__main__":
    main()
