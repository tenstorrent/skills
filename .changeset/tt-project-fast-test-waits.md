---
"tt-project": patch
---

`tt-project`: the runtime's wait loops (run supervisor, `ttp lock`, `ttp listen`, stop and
restart waits) read their poll interval through one helper that a test-only environment variable
can shorten. Nothing changes when it is unset. The runtime test suite uses it, and waits on files
and events instead of fixed sleeps, and runs in well under half the time.
