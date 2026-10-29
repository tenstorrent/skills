---
"tt-project": patch
---

`tt-project`:

- checks: a detached `ttp checks` that is still waiting for its lane, checks slot or test slots when the runtime is upgraded now re-executes itself with the installed runtime (once the new version has been on disk for 10 s), so it does not keep running old code after an upgrade. It keeps its pid, its checks lane (the lock file stays open through the exec), arrival and priority, gives back its checks slot and queue places to take them again at its arrival, and logs one line. A run whose commands have started is never re-executed. Test slots now queue at the run's arrival too, and a cancel (`checks-slot.cancel`, a reaped waiter) now also ends a wait for test slots, freeing the run's lane.
