---
tt-project: patch
---

A PR size budget: past `review.pr_budget_lines` (the repo norm's p90, else 800 added non-test
lines) a PR's branch takes no further commits on any route that updates it, and one keyed event
tells the coordinator to post a plan; `ttp pr plan <repo>#<n> --budget N --msg <id>` records the
plan that lifts the gate.
