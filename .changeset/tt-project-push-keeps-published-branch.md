---
"tt-project": patch
---

`ttp push` no longer rewrites a checked-out branch that is already on the remote (for example a task branch published with `ttp push --own`). The rebase, version bump and push run on a detached copy, the branch stays where it was, and the worktree returns to it afterwards, so a later `ttp push --own` still fast-forwards. A detached push reports the commit it pushed. A branch the remote does not have is rebased in place as before.
