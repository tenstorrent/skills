---
"tt-project": patch
---

A push claim whose head was rebased before landing is confirmed from the task's own push record, or from that commit or a rebased copy of it being on the push branch, instead of being reported as a push missing from the remote. The task's other landed work is no evidence for a claimed commit that never left the worktree.
