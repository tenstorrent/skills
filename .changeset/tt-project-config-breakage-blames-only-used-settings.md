---
"tt-project": patch
---

The config-breakage alarm only blames a setting for check runs that could have used it. A spill setting
counts only runs on a spill host, and `checks.spill_wrap_cmd` only runs that ran behind its new value.
Runs that failed only with tests also failing on the base on the same host, tests quarantined as flaky,
or failures the tests gate proved new at the same config are not counted. When the user set the value,
the event says so and gives no undo; it still reports the numbers. Its task count now matches the threshold.
