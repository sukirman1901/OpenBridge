## Summary
What does this change and why? Link the issue (`Fixes #N`).

## Checklist
- [ ] `python3 -m unittest discover -s test -t .` passes (341+ tests)
- [ ] `python3 -m compileall -q bridge.py openbridge` is clean
- [ ] No new runtime dependencies beyond the Python standard library
- [ ] No secrets in the diff (tokens, `config.json`, DB/log dumps)
- [ ] Persisted data keeps compatibility: no schema change without a new
      versioned migration in `openbridge/adapters/persistence/sqlite/jobs.py`
- [ ] Documentation updated (`README.md` / `ARCHITECTURE.md` / `CHANGELOG.md`)
- [ ] Behavior changed? A regression test covers it

## Verification performed
Describe what you actually ran against a live OpenCode/Telegram setup, if any.
