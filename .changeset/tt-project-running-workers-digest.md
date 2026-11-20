---
"tt-project": patch
---

`tt-project`: the coordinator digest gains a `## Running workers` section, built once per turn from `state/runwatch.json`: one line of at most 160 chars per running run (task, step, files changed and those outside the spec, last check, spend of budget and share of wall used, steer read state, drift flags), only when something runs. `coordinator.live_view_chars` (default 1200, 0 turns it off) caps it, flagged runs first, then `+N more`. A session wake folds the runs whose step, files, check, steer state and flags are unchanged into one line, so its delta resends the section only when a row changed. Both coordinator prompts say what it shows and that a flagged run gets a `task_update` `spec` or a restart.
