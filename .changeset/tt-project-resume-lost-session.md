---
"tt-project": patch
---

`tt-project`: A run that a reboot, a host sleep or a lost supervisor cut short after real progress
(`budget.resume_lost`, default $0.50 or 10 min) now continues its Claude Code session in the same working
directory with a short prompt, instead of starting over. Codex and Cursor, a missing transcript or
worktree, and a resume that fails to start fall back to a fresh start.
