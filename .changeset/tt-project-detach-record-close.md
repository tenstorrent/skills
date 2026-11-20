---
"tt-project": patch
---

`tt-project`: a `ttp detach` job's record in the run's `detached.json` no longer stays `running` after its driver is over. The daemon closes records once a minute as `ended` (exit code and end time from the .rc) or `died` (no .rc and its job lock free), reading liveness from the job lock, never a reusable pid; `ttp detach --reap <name|.rc>` closes one at once (0 closed, 1 still running, 2 unknown). Lock holds taken by a `ttp detach --lock` job count as held only while that job's lock is held. Known gap, documented: a driver that died without an .rc, or ended while its probe still says not yet, raises no progress event.
