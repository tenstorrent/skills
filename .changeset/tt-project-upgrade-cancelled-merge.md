---
"tt-project": patch
---

Template upgrades no longer stall behind a merge task the project cancelled: such a task no longer
counts as the batching window's one task, and a host-side `ttp upgrade` applies the merge it left
committed, model-free, when that commit merges cleanly with the current template and main and the
result compiles and imports (a conflict still queues a task). A deploy takeover that defers (exit 75)
to a live merge task now waits for that task without spending its tries, and reports the owner as
stuck after 6 h.
