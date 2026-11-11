---
"tt-project": patch
---

Batch runs: a worker that starts a code task also takes up to `dispatch.batch_max` (5) related small
ready code tasks in the same run, on one worktree and branch, one commit per task. A failed task goes
back to the queue alone, the run's cost is split between the tasks pro rata, and `ttp status`, the web
app and the coordinator digest show the batch as one run with its task ids. Off with `dispatch.batch`.
