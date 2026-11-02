---
"tt-project": patch
---

`tt-project`: cancelling a task stops its running checks right away, attached or detached. On the next daemon tick after a cancel (and in the 30 s job sweep as a backstop), every checks lane holder, queued checks ticket and detached checks run of that task is matched by its `TTP_TASK`/`TTP_TASK_ID` and recorded process group; the group gets SIGTERM, then SIGKILL after 5 s, and its checks lane is freed in the same step. A group holding another task's process, the daemon's own processes or an open review's prechecks is never touched, and a group that has already ended is no error.
