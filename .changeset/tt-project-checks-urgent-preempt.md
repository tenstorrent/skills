---
"tt-project": patch
---

`tt-project`: an urgent task's `ttp checks` no longer wait long behind routine checks when the host-wide checks queue and its urgent slot are full. A waiting routine run whose task turns urgent notices within 30 s and re-executes once as an urgent run at the same arrival (before, it kept the priority it started with). Preemption now waits a grace of `checks.preempt_after_s` (account-level, default 120 s) from the urgent run's arrival, asks exactly one routine run per request (its own project's first, then the least urgent, then the latest started), and never asks a push check, an urgent run, or a run behind a lock (`checks.wrap_cmd`, `ttp lock`, `ttp devq`, `flock`). The stopped run requeues at its old arrival, keeps what it recorded, reruns to its result, and its log, task notes and queue entry say `preempted by urgent #<task>`.
