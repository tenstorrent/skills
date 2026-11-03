---
"tt-project": patch
---

`tt-project`: a `ttp checks` record of another PID namespace that never wrote an outcome (its sandboxed run was killed) is pruned once its last write is 24 hours old (four times the default checks wall-cap ceiling), so `ttp status` no longer lists it forever as an "unknown (other PID namespace)" follower; a fresh one is kept. A device-queue wait alert names a holder pid it cannot place as "unknown (other PID namespace)" instead of "no pid recorded yet".
