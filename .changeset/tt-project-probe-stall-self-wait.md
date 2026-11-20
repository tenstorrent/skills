---
"tt-project": patch
---

`tt-project`: the progress check's probe stall no longer wakes the coordinator about a task's own self wait (`wait_kind` self). It now reads the waiting hand-off the daemon really records (`db.waiting_handoff`: status queued, last result waiting, no start_when deferral) instead of a task status `waiting` the daemon never writes, so the skip never applied. External and user-unrelated waits, and start_when deferrals, are still judged.
