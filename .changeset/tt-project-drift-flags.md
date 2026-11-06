---
"tt-project": patch
---

`tt-project`: running worker and reviewer runs get model-free drift flags from the live run record: a command that keeps failing, the same tool call in a loop, files changed outside the paths the spec names, a quiet run (no call, note or commit while not waiting on a lock, checks or a detached job) and spend running ahead of the wall clock. Each flag first goes to the worker as an update naming its evidence; still flagged 10 minutes later it rides along to the coordinator's next turn as a `worker_drift` event; only a run past 90% of its budget or a command failed 6 times wakes the coordinator. Thresholds are under `coordinator.drift.*` (0 turns a flag off). `ttp runs live` and the run record show active flags.
