#!/usr/bin/env sh
# OpenBridge one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/sukirman1901/OpenBridge/main/install.sh | sh
#
# Downloads the latest repo tarball into ~/OpenBridge, then hands off to
# the interactive wizard (wizard.py) which asks for your Telegram bot token,
# owner id and bot name, and writes a safe config.json.
# Re-running with an existing ~/OpenBridge just re-invokes the wizard
# (it never overwrites an existing config without asking).
set -eu

REPO_URL="https://github.com/sukirman1901/OpenBridge/archive/refs/heads/main.tar.gz"
DEST="$HOME/OpenBridge"

say() { printf '\n%s\n' "$1"; }

say "🌉 OpenBridge installer"

if ! command -v python3 >/dev/null 2>&1; then
    say "❌ python3 is required but was not found.
   - Debian/Ubuntu:  apt install python3
   - macOS:          brew install python3 || xcode-select --install
   - Termux:         pkg install python"
    exit 1
fi

if [ -d "$DEST" ]; then
    say "♻️  $DEST already exists — skipping download, running the wizard on it."
    cd "$DEST"
else
    TMP="$(mktemp -d)"
    say "⬇️  Downloading OpenBridge (latest main)…"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$REPO_URL" -o "$TMP/openbridge.tar.gz"
    else
        wget -qO "$TMP/openbridge.tar.gz" "$REPO_URL"
    fi
    mkdir -p "$TMP/extract"
    tar -xzf "$TMP/openbridge.tar.gz" -C "$TMP/extract"
    mv "$TMP/extract/OpenBridge-main" "$DEST"
    rm -rf "$TMP"
    say "✅ Installed to $DEST"
    cd "$DEST"
fi

say "🧙 Starting the setup wizard…"
python3 wizard.py
