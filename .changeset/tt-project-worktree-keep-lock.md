---
"tt-project": patch
---

`tt-project`: a task's worktree can no longer be orphaned by a `git worktree prune` that cannot see its folder (for example one run in a sandbox that mounts only part of the tree). Before, such a prune deleted the worktree's git record (`.git/worktrees/<name>`) although its folder, branch and hand-off files were intact, and `git worktree repair` cannot make a deleted record again. Now the daemon locks each task worktree with the harness's own reason when it hands it to a run, and the worktree sweep locks older ones it finds unlocked; git never prunes a locked worktree. The sweep lifts that lock only just before it removes the worktree itself; any other lock still keeps a worktree. A worktree whose folder is really gone and that the harness did not lock is still pruned. The setup-box skill now says to remove only your own worktree and never to run a bare `git worktree prune` on a shared clone.
