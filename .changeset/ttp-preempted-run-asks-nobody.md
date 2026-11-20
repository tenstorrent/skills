---
"tt-project": patch
---

ttp checks: an urgent run that was itself preempted no longer asks another run to step back when it queues again at its old place, so it cannot take a third run's slot ahead of the run that preempted it.
