---
"tt-project": patch
---

Heal checks: a check's self-fix task is found by its exact label, so `_` or `%` in a check name no longer match another check's task. A queued self-fix task is cancelled when its check recovers before the fix ran (a running one is left alone). A heal check that exits an error code gets one task: the self-fix escalation adopts the daemon's open `schedule_fix:<name>` task, and the daemon queues no schedule-fix task for a heal schedule whose self-fix task is open.
