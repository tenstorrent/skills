---
"tt-project": patch
---

`tt-project`: a run whose task carries the `untrusted_input` label gets three model-free guards on every provider (untrusted.py, wired into the worker hook, the run's `bin/git` pre-push hook and `bin/gh`): git push updates only the task's own branch, and the push branch only through `ttp push`; gh and curl writes are refused unless the task also carries `untrusted_write`; reads of credential, token or secret files outside the worktree and environment dumps are refused. A run without the label is unchanged. `ttp.untrusted.guards_live()` tells routing code the guards ship. Also, a worker's `pytest -n $VAR` (a count the shell fills in) outside `ttp clip` or `ttp sandbox` is refused like a literal count above 1, and worker.md says where a parallel pytest runs.
