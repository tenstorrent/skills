---
"tt-project": patch
---

`tt-project`: each project's landing check time (its push checks' wall time) is measured: `ttp status` and the daily review show the median and p90 over the last 24 h and the last 10 runs, and a time over `checks.smoke_max_s` (default 600 s) sends the project's coordinator one observation a day naming the slowest tests and suggesting a smoke split. New setting `delivery.post_land_checks` (same form as `push_checks`) for the slower tests that run after landing. The repository's tests gain a `tt_smoke` marker from a checked-in list (`tests/tt_project_smoke.txt`).
