---
"tt-project": patch
---

ETAs for tasks gated on a landing or a merge no longer show the probe's next check time. A task whose
start_when or waiting hand-off retry_when is `landed:#N` or `ttp landed --task N` follows #N's own
critical path (its review, fix or push) while something open can still land it. When nothing can (#N
failed, was cancelled or abandoned, or is done with its last review failed or its push row failed and
nothing open carrying it on), `ttp eta`, STATE's Expected finish, `ttp status` and promise ETAs read
"after landed:#N (why)" with no clock time. A `gh pr view`/`gh api .../pulls/N` state gate reads
"after PR #N merges or closes" unless the hand-off gives a measured eta.

New must-act event `order_landing_stuck`: an open order or promise whose owner waits on a landing that
nothing can make (the gated task done and not landed, its latest review failed or superseded or its
push row failed, no open task continuing it, past `waiting.landed_wait_dead_s`) raises one event per
episode naming the order, the owner, the gated task and the failed review. It is retired once a fix,
re-review or push carries the change on, or when the promise closes.
