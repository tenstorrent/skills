---
"tt-project": patch
---

`tt-project`: the push tests gate of a branch not yet on the remote (no push target tip handed to it) now reruns its failing tests on the merge base with the branch's nearest base (the push target, else `delivery.base_ref`, else the remote's default branch) instead of on no base, so a failure the base has too no longer blocks the push; a failure new on the head still does.
