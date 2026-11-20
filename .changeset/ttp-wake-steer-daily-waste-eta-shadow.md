---
"tt-project": patch
---

Two more things behind the existing `coord_wake_settle` flag (off by default). The daemon runs the waste ledger once a day without a model, in every project, whether or not the daily review runs. A coordinator wake of 8 or more tool calls steers the next wake to batch its reads into one shell call or hand reading to a worker. A waiting task whose spec was edited only to record that its probe passed no longer wakes a model for a spec change; the edit becomes its new baseline and the pass is settled as before. `ttp status` and the daily review show how many wakes made 8+ tool calls and their cost, flag on or off. The `eta_history` flag now gates a shadow: each ETA is logged beside a history-based estimate, nothing users see changes, and `ttp eta --calibration`, `ttp status` and the daily review show the 7-day baseline (ETAs compared, median and p90 absolute error of each method).

rollout: flag:coord_wake_settle flag:eta_history
