---
"tt-project": patch
---

`tt-project`: a merge that rewrites a harness checkout keeps a whole copy of the runtime from before, one copy per hold, and the launcher runs the newest copy whose holder lives while it notes each process it starts on it; a process that imports ttp from a hold copy notes itself too, so a listener or a child never loses the files under it. Every harness git writer that can rewrite `runtime/` now runs under the same hold, a harness whose `.git` is a file (a linked worktree) holds its runtime too, and the hold marker lands with its copy so a concurrent hold cannot sweep it mid-merge.
