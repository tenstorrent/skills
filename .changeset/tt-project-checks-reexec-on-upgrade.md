---
"tt-project": patch
---

`tt-project`:

- checks: a detached `ttp checks` that is still waiting for its lane, checks slot or test slots when the runtime is upgraded now re-executes itself with the installed runtime (once the new version has been on disk for 10 s), so it does not keep running old code after an upgrade. It keeps its pid, arrival and priority, releases what it held before re-executing and logs one line. A run whose commands have started is never re-executed. Test slots now queue at the run's arrival too.
