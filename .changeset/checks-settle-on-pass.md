---
"tt-project": patch
---

Checks waits: a task waiting on its own `ttp checks --result <run dir>` now wakes a model only on a
failure, a broken probe or a timeout. A waiting hand-off on such a wait that names no `on_pass` or
`on_pass_cmd` and asks for nothing after the pass gets the new on_pass_cmd
`ttp checks --settle <run dir> --head <sha>` filled in: on a pass of that head, still checked out
clean, it records the hand-off's `on_pass` or a `done` hand-off from its summary with no model run; a
failure wakes the task as before and a pass on another head records nothing. Hand-offs that ask for
work after the pass (a `next_step`, a PR, a review verdict, a landing) keep waking on it.
`ttp checks --result <run dir> --passed` exits 0 only when the checks passed, for hand-written
on_pass_cmds (plain `--result` exits 0 on a failure too). Documented in the worker prompt, the guide
and the harness skill.
