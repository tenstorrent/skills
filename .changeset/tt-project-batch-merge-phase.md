---
"tt-project": patch
---

Push batches wait for a running tt-project upgrade once per round, before the merge loop, instead of once per entry. The batch log and marker record the seconds of each phase (merge, entry checks, upgrade waits, checks, push, deploy). A batch's after_push deploy no longer holds the next batch: the next batch merges and checks while it runs. Deploys still run one at a time, and a newer waiting deploy supersedes an older queued one.
