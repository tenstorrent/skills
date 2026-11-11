---
"tt-project": patch
---

`tt-project`: the Slurm checks overflow's breaker now tells a re-arm by the settings log line it began under, not by comparing timestamps. Before, the log's millisecond-rounded stamp could look newer than a fresh breaker record started just after the user re-armed it, so each job that died at start reset the count and the overflow never paused again; and a trip stamped after the user's setting (a clock step) was not re-armed by it. Breaker records written by earlier versions are still read by their timestamps.
