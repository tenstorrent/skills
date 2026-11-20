---
"tt-project": patch
---

`tt-project`: the dispatch dollar-cap check (`Daemon._fits_caps`) now measures the daily cap over the budget day (`spent_today`) once `budget.day_start` is set, the same window the budget gate uses. Before, it used the rolling 24 h spend, so work stayed held as over the cap while the budget day still had room. The weekly cap still uses the last 7 days. New `budget.cap_yellow_pct` (default 60) sets the share of a dollar cap at which the gate turns yellow and halves the worker slots.
