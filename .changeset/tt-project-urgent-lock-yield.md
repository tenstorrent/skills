---
"tt-project": patch
---

`tt-project`: a user-ordered urgent task kept off a resource by another run's `ttp lock` or its task cap asks each holder to yield on the next daemon tick: the resource is reserved for it, so it takes the lock ahead of older waiters, and a run of the project still holding it after `budget.urgent_yield_grace_s` (default 180 s) is stopped with its work kept and no attempt spent, then resumes; a detached job of the project's own holding it (`ttp detach --lock`) is ended and its task's wait wakes it. A person's hold, another project's run, a paused or user-held resource and routine priority-1 work are never stopped. Each request and stop is logged with who, for whom, the resource and when, and an urgent task's start wakes the coordinator at once (`urgent_started`).
