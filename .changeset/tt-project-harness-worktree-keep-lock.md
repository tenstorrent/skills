---
"tt-project": patch
---

`tt-project`: a harness task's own worktree (of the harness repo) is now locked like a task worktree when the daemon hands it to a run, so a `git worktree prune` that cannot see its folder no longer deletes its git record and orphans the run's work. The landing lifts only that lock just before it removes the worktree; any other lock keeps the worktree.
