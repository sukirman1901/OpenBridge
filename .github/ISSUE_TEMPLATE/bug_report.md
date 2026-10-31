---
name: Bug report
about: Something breaks or behaves unexpectedly
labels: ["bug"]
---

### Summary
A clear one-paragraph description of the bug.

### Reproduction steps
1. Configure … (redact tokens and owner IDs!)
2. Run `./start.sh` (or `./supervise.sh &`)
3. Message the bot with …

### Expected vs actual
Expected: …
Actual: …

### Environment
- OS / container (e.g. Ubuntu proot in Termux, plain Ubuntu):
- Python version (`python3 --version`):
- OpenCode version (`/api/info`):
- Relevant log tail (**redact tokens, phone numbers, chat contents**):
```text
(paste here)
```

### Notes
Never paste real tokens or credentials — `*.token`, `config.json`, and the
database are already excluded from the repository for a reason.
