---
"tt-project": patch
---

`tt-project`: a code task on a batch run's branch is no longer held by a member of that batch once the batch's lead is in review or pushing (the member stays `running` with no worker until the batch lands, so a fix on the branch would wait for a batch that waits for it). A task held for another reason (disk guard, review precheck) drops its stale `held: waits for #N` branch note. `carryback.reworks` walks a `follows:` hop to a done code task like `continues:`, so a follow-up of a passed fix is not sent back for the commits that fix re-worked (note 6d2731bfdd957b75). The added-tests rerun drops the check's own `-m` and `-k` filters, so a smoke check still reruns its added tests, and a rerun whose tests ran no round is reported as skipped, never passed.
