---
"tt-project": patch
---

`tt-project`: push checks now get `TTP_PUSH_BRANCH`, the name of the branch the push goes to on the remote, in short form without `refs/heads/`. It is the push branch for `ttp push` and push-queue batches, the task's own branch for `ttp push --own`, the local branch for `ttp land`, and empty for `ttp checks`. A check can now refuse content by where it is going, even when the checked-out branch has another name.
