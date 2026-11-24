---
"tt-project": patch
---

`ttp checks` and its spill copy drop a caller's GIT_DIR, GIT_WORK_TREE and other repository variables
once they have found the worktree: a worktree of a bare mirror that borrows its objects (alternates)
now runs its own repository's checks in a clean checkout, instead of the harness's checks failing with
no `.git/objects/info/alternates`, and the spill bundle no longer stops at `git init --bare` exit 128.
A failed bundle step reports git's own error.
