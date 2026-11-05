---
"tt-project": patch
---

`tt-project`: a spec update to a task asleep on its retry_when no longer wakes it just because the task is priority 1: only a task_update wake_now or an update that adds an `urgent:` line wakes it early, and an added `urgent:` line wakes it once per wait. Obligation notes appended to a spec change no spec digest, so promise bookkeeping never wakes a worker.
