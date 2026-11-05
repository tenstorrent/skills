---
"tt-project": patch
---

A plan's start condition holds every task it queues: a plan hand-off's `gate` (depends_on and/or a start_when probe) is stored on the plan, and task_add of a task citing `from plan #<id>` inherits it, ANDed with its own. Tasks already queued from the plan are held when the gate is recorded (the hand-off or `task_update` `gate`). A gate that cannot be applied whole comes back as a note. A hold the user ordered raises no lane_wait event per held task.
