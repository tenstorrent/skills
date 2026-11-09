---
"tt-project": patch
---

Each project runs an overseer beside its daemon, as its own service: it judges the tick loop by
its results (last dispatch, urgent work waiting with a free slot, unreaped runs, unapplied
coordinator answers, undelivered events, slow steps per hour) from a read-only database view, and
shows them as one `loop health:` line in the coordinator digest, `ttp status` and `ttp deployed`.
A results failure saves the daemon's stacks and records one incident; the daemon restarts an
overseer that stopped.
Urgent work a hold keeps out on purpose (a paused project, a closed budget gate, a paused or held
resource, a review waiting on the prechecks the daemon runs on its head) is named as held, not
counted as a results failure.
