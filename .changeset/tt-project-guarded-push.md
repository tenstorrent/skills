---
"tt-project": patch
---

`tt-project`: new `ttp push` publishes a worktree's commits onto the project's target branch,
guarded. It refuses uncommitted changes, rebases onto the latest tip (aborting on conflict), runs
the project's `delivery.push_checks` on the exact commit it pushes, starts over if the branch moved
during the checks (up to `delivery.push_rounds`, default 3), and pushes without force. The target
is `delivery.push_branch`, else `delivery.base_ref`; with no target or no checks it refuses. Review
tasks are told to push only through it, and the coordinator may set both keys.
