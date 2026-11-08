---
"tt-project": patch
---

`tt-project`: the guide documents how long an urgent checks run that preempted a routine run while it waits for its test slots keeps its claim on the freed slot (up to 60 s from its ask, so a run slower than 30 s to end its command still hands the slot to it), with a test of that case.

`tt-project`: a waiting checks run whose task's priority changes (raised or lowered) re-queues at the new priority, keeping its place, and an owed or urgent run is never held by a less urgent run that has waited long.
