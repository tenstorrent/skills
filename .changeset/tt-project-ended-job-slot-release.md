---
"tt-project": patch
---

`tt-project`: a lock slot held for a detached job that wrote its .rc, has no process left and whose task is done, failed or cancelled is freed: the daemon's stale-lock sweep now reaps a leftover of the holder's process group that keeps it (before, any live group member counted as a legitimate hold, so the slot stayed taken forever), and `ttp detach --reap` and `ttp detach --stop` on that job free it at once. A live holder, a holder this host cannot judge (another host or PID namespace), a job still running and a task still open or queued to retry keep the hold. Each release is logged as a `lock_released` event saying the job and task ended.
