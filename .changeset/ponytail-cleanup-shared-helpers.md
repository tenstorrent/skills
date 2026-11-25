---
"tt-project": patch
---

`tt-project`: review #9779's duplicate code is gone, with no behaviour change. A missing-entry must-act item states its rule once (the event's own line; the extra must-act text is dropped); one helper in `eta.py` computes a promise's due/made, whether it is still ahead and justified, for both the promise-slack alarm and the long-promise course lines; one helper gives `check_lock_yield` and `check_urgent_start` the same urgent-task query, lock-holder walk and own-hold rule, so the two agree on which holds are the urgent task's own.
