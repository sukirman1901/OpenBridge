# Contributing to OpenBridge

Thanks for your interest in making OpenBridge better! 🎉 This document explains how to set up, what the project rules are, and how to get changes merged.

## Project rules (please respect these)

1. **Python standard library only** — no third-party runtime dependencies. `bridge.py` must keep working in minimal environments (Termux/proot, containers, CI). If you need a library, ship it as an optional integration, not a hard requirement.
2. **Never commit secrets** — that includes bot tokens, API keys, personal tokens and logs that contain them. Use the `{file:botname.token}` token indirection in `config.json`; `*.token` is already in `.gitignore`.
3. **English in code** — comments, docstrings, log messages, and user prompt strings should be in English so the project stays easy to review internationally.
4. **Config keys are a schema** — keys like `nama`, `cooldown_detik`, `initiatif_detik`, `max_per_menit`, `budget`, `owner_ids` are loaded by name from `config.json`. Renaming them is a breaking change; discuss in an issue first.

## Development setup

```bash
git clone https://github.com/sukirman1901/OpenBridge.git
cd OpenBridge
python3 -m unittest discover -s test -t .   # all tests must pass
python3 -m compileall -q bridge.py openbridge
```

No `pip install` is needed — tests run on the standard library alone.
Monolith-era tests under `test/legacy/` are retained until their
uniquely-covered cases are ported to the modern suite; they still run.

## Making changes

1. Open (or claim) an issue describing the bug or feature first — bigger changes deserve discussion before code.
2. Create a feature branch: `git checkout -b feat/my-change`.
3. Keep changes focused: one logical change per PR. If you touch behavior, update or add unit tests in the matching file under `test/`.
4. Run the full test suite before pushing.
5. Open a PR with a short description of **what** changed and **why**. Link the related issue (`Fixes #123`).

## Where to start

Good first contributions: parser edge cases, README/docs polish, test coverage for `bridge.py` helpers (natural-language schedule parsing, mode-token parsing), or packaging helpers (systemd unit, Dockerfile).

## Reporting security issues

Please do **not** open public issues for security problems that touch tokens, secrets, or the bridge's auth flow. See [SECURITY.md](SECURITY.md) for the private reporting path.

## Code style

- The repo deliberately has near-zero formatting dependencies: keep lines reasonably short, follow the surrounding style (`snake_case` functions, module section banners like `# ── Section ─────`).
- Prefer small pure functions over class hierarchies; the codebase is utility-style by design.
