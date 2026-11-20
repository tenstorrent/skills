---
"tt-project": patch
---

`tt-project`: the daemon's job sweep no longer stops or kills the detached jobs of a task that is still alive. Before, a task that handed off `blocked` lost every detached job its hand-off did not name in a form the sweep recognised, even when its retry_when waited on those jobs' results. Now the handed-off sweep reaps only a `done` task's jobs (still keeping any its hand-off names or an open task waits on); a waiting, blocked, queued or running task keeps all of them. Cancelled, failed and exhausted tasks' jobs are reaped as before. A `blocked` hand-off made while the task's own detached jobs still run is recorded as waiting: it keeps its retry_when, or else waits for those jobs to end, and wakes at standard with its question kept.
