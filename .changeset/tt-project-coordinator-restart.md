---
"tt-project": patch
---

`tt-project`: the coordinator can restart a running worker that went the wrong way without losing its work. A `task_update` whose `spec` has a `restart: <why>` line, or `ttp task <project> restart <id> "<why>"`, stops the run at a safe point (a run holding a `ttp lock` stops once it releases it), requeues the task with the updated spec, its worktree, branch and attempts kept, and starts the next run's prompt with the why and the new instructions. A restart spends no attempt and is not a failure. At most `budget.restart_max` (3) restarts per task per `budget.restart_window_h` (6 h); a later one is refused with the reason. Each restart is logged with its why. A plain spec update still only reaches the running worker through its `steer.md`.
