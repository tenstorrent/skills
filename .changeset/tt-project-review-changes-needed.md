---
"tt-project": patch
---

A review that hands off `done` with a `metrics.verdict` asking for changes (`changes_needed`, `changes_requested`, `rejected`) is its own outcome, `changes_needed`, not a failed task. It still gates the change: nothing is approved, and the daemon's fix task and re-review follow as before. But it spends no attempt, never retries or raises a tier, counts as a correct effort pick, and stays out of failure counts and the repeated-failure and resource-trouble rules. `ttp status`, `ttp task list`, the coordinator digest and the web app (`outcome`, `task_counts`) show and count it apart; the event is `task_changes_needed`.
