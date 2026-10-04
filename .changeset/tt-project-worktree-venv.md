---
"tt-project": patch
---

Workers and reviewers run with the project's Python venv active (`VIRTUAL_ENV`, `PATH` after the harness `ttp`), so a fresh task worktree does not rebuild one. New config key `worktree.venv`: `auto` (default) finds `.venv` or `venv` in the project root, a path names another, `""` turns it off. A working directory with a venv of its own keeps it, and a venv whose interpreter is gone is skipped.
