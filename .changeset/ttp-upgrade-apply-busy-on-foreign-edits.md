---
"tt-project": patch
---

`tt-project`: `ttp upgrade <name> --apply <commit>` now exits 75 (busy, nothing was changed) when the live harness checkout holds uncommitted changes it did not make to files the fast-forward would write (a tracked edit, a staged edit, or an untracked file where the upgrade adds one), such as another harness task's edit made while the upgrade task held the harness. Before, git refused the fast-forward and the upgrade failed with exit 1. The message names the files; the upgrade task's steps say to keep its worktree, leave those changes alone and rerun the step later. Uncommitted edits to files the upgrade does not write, such as the charter and memory, still apply as before.
