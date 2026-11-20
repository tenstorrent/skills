---
"tt-project": patch
---

`tt-project`: a project's test-worker ceiling (`checks.test_project_workers`, when neither the account nor `TTP_TEST_PROJECT_WORKERS` sets it and `checks.load_extra` is not 0) grows into spare cores: the base (the smaller of 8 and one per 8 cores, at least 2) plus the cores the larger of the 1- and 5-minute load averages leaves under 0.75 x the cores, at most half the cores. On 64 cores that is 32 workers at load 0 or 16, 24 at load 32 and 8 at load 48, so one project runs several suites at once on a lightly loaded host. A project's own setting still only lowers it; when the ceiling shrinks, running suites keep their workers and still count, so new grants stay within the new ceiling.
