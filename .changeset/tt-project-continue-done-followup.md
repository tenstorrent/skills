---
"tt-project": patch
---

`task_add` with `continues` on a done task adds a follow-up (parent = the done task) instead of
being rejected. It takes over no dependents, and a code follow-up starts from the base branch,
not the old task's branch. `schedule_set` takes a bare integer `every` as seconds and echoes it.
