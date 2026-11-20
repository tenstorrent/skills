---
"tt-project": patch
---

A plan's start condition holds every task it queues: a plan hand-off's `gate` (depends_on and/or a start_when probe) is stored on the plan, and task_add of a task citing `from plan #<id>` inherits it, ANDed with its own. Tasks already queued from the plan are held when the gate is recorded (the hand-off or `task_update` `gate`). A gate that cannot be applied whole comes back as a note. The gate's why is kept verbatim and counts as the user's word only when it cites a user message; lane_wait skips only the tasks a user-cited gate names, never a held task's other holders.
