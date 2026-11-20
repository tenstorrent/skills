---
"tt-project": patch
---

tt-project: a shared-branch member that fails review past its fix hop is reverted forward on the shared branch (a `Ttp-Reverted-Task` commit), requeued alone on a branch of its own, and the passing members are re-reviewed at the new head. A change that really conflicts with the integration branch (git merge-tree) or carries a `no_push:` line no longer joins a shared review: it is labelled `own_branch:<why>` and gets its own review.

The revert is all or nothing: if any failed member's revert fails, the shared branch is put back and nothing is requeued, re-reviewed or approved, and the no-fix wake names the member. The `Redo #N alone` task carries `no_pr_branch` and quotes the member's spec, so it never delivers onto the shared branch. Conflict routing resolves a remote-qualified push branch (`<remote>/<branch>`) the way push does.
