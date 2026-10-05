---
"tt-project": patch
---

`tt-project`:

- ttp task set-when re-points a not-yet-started task's probe
- wake the coordinator when a dependency sits in review too long (coordinator.review_stall_s)
- idle-slot wake backs off to long waits while every queued task is gated
