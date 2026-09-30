---
"tt-project": patch
---

`tt-project`: a task blocked on a cancelled or failed dependency can be unstuck.

- `task_update` takes `depends_on`: a list of task ids that replaces the task's dependencies; an
  empty list clears them. Unknown ids, a task depending on itself and cycles are rejected.
- Re-pointing a task blocked on a dead dependency queues it again, and it stays queued.
- A requeue that still depends on a cancelled or failed task is rejected instead of being undone
  silently a moment later; the reason reaches the coordinator's next turn.
- The blocked-task event says how to re-point or cancel the task.
