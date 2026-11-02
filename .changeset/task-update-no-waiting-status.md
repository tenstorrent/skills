---
"tt-project": patch
---

Coordinator: a task_update setting status `waiting` is rejected with guidance (queued with start_after
or start_when, or blocked with waits_on). The daemon keeps a waiting task as queued with its waiting
hand-off, so a stored status `waiting` was one nothing dispatched, probed or woke: the task was
stranded silently. A test also pins that several spec-only updates to a task asleep on its probe fold
into the one run its probe starts.
