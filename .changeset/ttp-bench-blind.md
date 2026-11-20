---
"tt-project": patch
---

`tt-project`: `ttp bench blind` runs the blind review (`python3 -m ttp.benchblind`) on a head-to-head: `ttp bench run` now also writes `cells.json` (per task and repeat whose arms all ran: the task id, the acceptance section of its spec and each arm's diff with its provider, model, effort, run id, session id and transcript path), and `ttp bench blind prep <run folder> --out <dir>` seals it for `review`, `score` and `reveal`, so the arms are scored without the reviewer knowing which model wrote them.
