---
"tt-project": patch
---

`tt-project`: a restart without systemd or launchd waits for the old daemon to exit before
starting the new one, and never rolls back the runtime while the old daemon is still alive.
Worktree pruning checks merges against the resolved base branch and keeps the worktree when
that branch cannot be found.
