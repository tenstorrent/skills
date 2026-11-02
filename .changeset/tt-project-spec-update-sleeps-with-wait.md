---
"tt-project": patch
---

`tt-project`: a spec-only `task_update` on a task asleep on its `retry_when` no longer starts a model run at once. The change sleeps with the task and applies when its wait ends (the probe passes, or any other wake such as a broken probe or a failed job), and that run reads it at the task's own tier. An urgent task (priority 1 or an `urgent:` spec line) still wakes at once, and a new `task_update` field `wake_now: true` wakes a sleeping task at once on purpose, with or without a spec change. Other holds (dependencies, start_after, start_when, a paused or busy resource, a gate) and the limit of 3 at-once wakes an hour per task still apply. A worker cannot set `wake_now` in its own hand-off. Updates to start_when, start_after, depends_on, resources or status behave as before.
