---
"tt-project": patch
---

`tt-project`: the daily model-bench suite can be calibrated against reference models. `model_bench.reference` (a list of `{"provider", "model", "effort"}`, default none) names them; each runs every task of a new task pair once, beside the day's arms, and its rows are reused while the pair is unchanged. Each standard row says whether the day's table is `calibrated` or `uncalibrated` (with why) and carries the references' rates on the same tasks, also in `<date>-cells/calibration.json`. `ttp model-bench suite --reference-from DIR` imports reference rows run elsewhere on the current pair.
