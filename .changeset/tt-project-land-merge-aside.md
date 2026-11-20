---
"tt-project": patch
---

Harness landing merges a task branch in a scratch worktree and only fast-forwards the harness checkout to a clean result, so a conflicting branch no longer runs `merge --abort` (a reset) in the checkout each time HEAD moves; uncommitted, untracked and staged work there stays.
