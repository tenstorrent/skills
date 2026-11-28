---
"tt-project": patch
---

A waiting task's probe runs the moment what it waits on ends: a local `ttp detach` job's `.rc` file, a detached push's result marker, a recorded landing of a task its probe names (`landed:#<id>`), as `ttp checks` already did, not after the poll gap; the probe's own verdict then wakes it with no model call. A task of an open user order is re-checked at most every 2 minutes (probe gap and timer re-sleeps), urgent or not; the poll stays as the fallback.
