---
"tt-project": patch
---

A run waiting on its own `ttp checks` is no longer woken by its timer, a spec change or a new progress line while those checks run and their log advances; checks that end wake it model-free at once, and checks that died or stalled keep the fallback wake. The hourly Tests row shows check runs and CPU-hours with no total spend, and apart, labelled, the model spend of runs woken while waiting on their checks (wakes, $ and wake reasons; `ttp stats --landing` too).
