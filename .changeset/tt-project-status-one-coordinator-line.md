---
"tt-project": patch
---

`tt-project`: `ttp status` prints one coordinator line instead of three: the route (`provider/model · effort`), when the last turn ran and its status, and `configured X, ran as Y` when the last turn ran with a different model or effort than configured. The separate route and coordmodel lines stay for `--verbose` and the daily review. Failure, backoff and idle-check suffixes stay.
