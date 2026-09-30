---
"tt-project": patch
---

`tt-project`: `task_add` takes `continues: <id>` for a failed, cancelled or blocked task. Its open
dependents move to the new task in the same transaction, and those blocked only by that dead task
go back to the queue. A continued blocked task is cancelled, so it cannot run twice. A code task starts from the old task's branch head, and its prompt names the
old task, branch and last summary. A block on a dead dependency still there after a coordinator
turn is raised once as an event that wakes the coordinator.
