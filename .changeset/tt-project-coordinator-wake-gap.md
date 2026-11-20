---
"tt-project": patch
---

`tt-project`:

- coordinator wakes: event-only batches with no immediate event (task ends, hand-offs, timers, sweeps, notes, waits) wake the coordinator at most once per `coordinator.wake_gap_s` (300 s) after the last turn ended, a free worker slot included; before such a wake the watcher model (babysit.ask, target coordinator) may fold it into the next wake, where the events stay queued and ride the next digest, capped by `coordinator.wake_triage_max_s` (1800 s). User messages, answered asks, must-act, high or critical, overdue obligations, safety and outage events wake at once; a watcher error wakes and logs the model and the error. The coordinator's effort tier never changes; tests, docs
