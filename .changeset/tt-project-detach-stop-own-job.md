---
"tt-project": patch
---

`tt-project`: `ttp detach --stop` lets a task stop its own detached jobs, from an earlier run or its current one, while the task is still open, and records who stopped the job, the run that started it, the caller's run and the reason (`--why`, else a default) in the job's `.lost` and stop event. Other tasks, and callers with no task, are still refused. A stopped job is never relaunched at boot.
