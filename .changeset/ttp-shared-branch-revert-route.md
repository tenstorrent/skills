---
"tt-project": patch
---

tt-project: a shared-branch member that fails review past its fix hop is reverted forward on the shared branch (a `Ttp-Reverted-Task` commit), requeued alone on a branch of its own, and the passing members are re-reviewed at the new head. A change that really conflicts with the integration branch (git merge-tree) or carries a `no_push:` line no longer joins a shared review: it is labelled `own_branch:<why>` and gets its own review.
