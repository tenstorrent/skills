---
"tt-project": patch
---

`tt-project`: a task that hands off `done` no longer leaves its detached jobs running and holding their locks. The daemon's jobstop sweep now also stops the live `ttp detach` jobs, across all of the task's runs, of a `done` task and of a `blocked` task whose hand-off names none of its jobs. A job the hand-off's `retry_when`, `on_pass_cmd`, `on_pass` or `next_step` names, or one an open task waits on, is kept. A waiter that names a job as `<run>/<name>` (the `ttp detach --check <dir>/<name>` form, no `.rc`) now counts.
