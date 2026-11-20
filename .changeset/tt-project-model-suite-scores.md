---
"tt-project": patch
---

`ttp model-bench suite` rows now carry `score` (the pass rate), `suite` and `inject_ok` (false when a planted instruction was followed), so the model score board reads them. `ttp model-bench suite --detach` starts the suite in the background with its options (log `suite.log`) instead of the daily bench.
