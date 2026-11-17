---
"tt-project": patch
---

`ttp eta` and the Expected finish give a likely time (p50) and a conservative time (p90) from the
project's own history by task kind and size, adjusted upward for current host load and queue depth,
and name the adjustments. A waiting hand-off or `ttp progress report --expected-wait-s` may carry an
expected wait for outside work, which the estimate uses. The ETA ledger's per-kind correction and
measured conservative multiple stay in force.
