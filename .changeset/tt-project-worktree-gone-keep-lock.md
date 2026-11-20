---
"tt-project": patch
---

`tt-project`: a task worktree the harness locked no longer holds its branch forever after its folder is deleted outside the harness (by hand or a disk cleanup). Before, git kept the locked record, so the branch stayed "already checked out" and could not be checked out anywhere else. Now the daemon's worktree sweep unlocks and drops such a record when its folder is missing from a readable parent under the worktrees or runs folder, and logs it. A missing or unreadable parent is treated as a partial view and the record stays; any lock the harness did not set is left alone.
