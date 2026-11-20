---
"tt-project": patch
---

Release-flag health watch: a shadow flag (`shadow` in the registry and in `ttp rollout status`) is judged on no health signal (cost, time to land, failed runs, alerts, reverts, stuck work), so the watch never rolls it back; a rollback by hand still works. Cost, time-to-land and failed-run checks need 20 landings by default (`thresholds.min_landings`) and compare each task kind and tier with its own baseline, leaving out kinds the baseline lacks; the rollback names the metric, the sample and the per-kind figures.
