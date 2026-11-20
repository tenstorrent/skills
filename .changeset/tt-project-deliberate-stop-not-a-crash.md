---
"tt-project": patch
---

tt-project: a worker run its STOP file asked to end for good (a cancel, a superseded or parked run) whose supervisor died before recording its end now ends as a deliberate stop, never restarted free as a crash (lost_to_crash). Runs with no STOP file, or one a project stop or preemption wrote, restart as before.
