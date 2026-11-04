---
"tt-project": patch
---

`tt-project`: a `ttp lock` waiter running inside a landing's checks (`ttp push` to the push branch, the push queue's batch checks, `ttp land`), from any project, goes ahead of non-landing waiters on a shared lock, in arrival order within each class. Only the harness marks a landing: the check runner holds a landing mark and `ttp lock` counts it only when an ancestor holds it, so a forged marker gets normal ordering. A waiter of an hour or more is never passed by later landings, and no holder is preempted. The `ttp lock` wait line says whether the waiter and the holder are landings.
