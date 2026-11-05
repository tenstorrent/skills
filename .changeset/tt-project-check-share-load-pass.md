---
"tt-project": patch
---

Checks: while other runs of a project wait, one full run takes only its share of the project's test
workers (at least `checks.test_run_min`, else the ceiling divided by `checks.test_project_max`), so a
second suite starts beside the first. Over the host load guard, one push-queue or urgent run at a time
still starts; routine runs wait. `ttp checks --slot-status` names the gate that holds each parked run.
