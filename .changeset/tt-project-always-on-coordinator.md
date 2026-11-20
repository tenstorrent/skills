---
"tt-project": patch
---

`tt-project`:

- always-on coordinator: the session spend guard and every stateless or fresh-context coordinator path are deleted (Claude Code, Codex, Cursor); every wake resumes the project session or starts a seeded one; dollar caps and task budgets are the only cost limits; retired coordinator.session.mode and pause_h keys are refused
- a daemon restart or host reboot resumes the same coordinator session; full context compacts in place instead of rolling over; tests
