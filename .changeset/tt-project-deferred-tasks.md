---
"tt-project": patch
---

`tt-project`: The coordinator can defer a task instead of keeping the deferral in a memory note.
`task_add` (and `task_update`, for a task not yet started) accepts `start_after` (a delay such as `3d`
or an ISO time) and `start_when` (a read-only shell probe run from the project root: exit 0 starts the
task, 1 means not yet). The task stays queued and undispatched until then. The probe runs model-free in
the waiting-probe runner, after any `start_after`. A broken probe (another exit, a timeout, or a probe
that cannot start) raises one event to the coordinator and never a worker run; so does a `start_when`
still not met after `coordinator.defer_max_days` (default 14). Status, the web app and the coordinator
digest show "starts <time>" or "starts when: <probe>" in the task row, not as an alert. The action
schema checks `start_after`'s form. Follow-ups may carry the same fields, and the daily review turns
deferrals held in memory into deferred follow-ups that the coordinator adds while retiring the entry.
