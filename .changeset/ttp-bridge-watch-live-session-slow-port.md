---
"tt-project": patch
---

The bridge self-heal watch no longer restarts a live bridge session because one
of its forwards is slow: a session holds all its forwards or exits, so when any
of its ports answers, a silent one is a slow or refusing destination, and a
restart would only drop every other forward for half a minute.
