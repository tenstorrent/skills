---
"tt-project": patch
---

`tt-project`: `ttp checks` leaves no process or clean tree behind. Each check carries the run's token in its environment; once its exit code is known, and when it fails, is stopped or the runner gets SIGTERM, SIGHUP or SIGINT, every process still carrying the token (or in the check's group with its working directory in the tree) gets SIGTERM, then SIGKILL after a short grace, and the clean tree is removed. A run records itself in the project's state (`check-sandboxes/<token>`, with `exit.json` once its checks end); a model-free sweep at each run's start and in the daemon's housekeeping reaps earlier runs' records that ended or whose runner died, past a grace period, signalling only processes it can tie to that record, never a reused pid. A running record is left alone. checks.log says how many processes and trees were reaped.
