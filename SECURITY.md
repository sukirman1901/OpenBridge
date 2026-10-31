# Security Policy

OpenBridge is a Telegram bot bridge that holds **bot tokens** and talks to a language-model API. If you found a way to leak those, bypass the owner check, or break the bridge, please help us report it responsibly.

## Reporting a vulnerability

**Do not open a public GitHub issue for security reports.** Instead:

1. Email: see the repository owner's profile page for a contact address, or
2. Use GitHub's **"Report a vulnerability"** button on the Security tab (Private vulnerability reporting).

Please include:
- A short description of the issue
- Steps to reproduce (a minimal snippet or config is ideal)
- Which parts are affected (bridge auth, token handling, scheduler, task queue…)
- Any suggested fix (optional, but very appreciated)

## What we promise

- We will acknowledge reports within 7 days.
- We will keep the report private while working on a fix.
- We will credit you in the release notes/changelog if you wish.

## Scope notes

- The bridge is designed to be **owner-only**: non-owner chats are fail-closed and cannot trigger destructive actions. Reports about bypassing the `owner_ids` policy are in scope.
- Prompt-injection attempts against the language model (users in groups trying to make the model call tools or leak memory) are **in scope** and welcome — please describe how it degraded the guardrails.
- The codebase is Python stdlib-only; reports about third-party dependencies are out of scope because there are none.
