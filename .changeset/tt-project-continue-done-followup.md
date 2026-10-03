---
"tt-project": patch
---

`task_add` with `continues` on a done task adds a follow-up (parent = the done task) instead of
being rejected. It takes over no dependents, and a code follow-up starts from the base branch,
not the old task's branch. `schedule_set` takes a bare integer `every` as seconds. Both changes
are reported to the coordinator as notes in its next digest, never as rejections: replies get no
"(not done: ...)" and no `rejected_actions` event is logged.
