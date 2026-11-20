---
"tt-project": patch
---

`tt-project`: fewer extra worker runs around checks, pushes and reviews. A spec change or a changed probe answer no longer wakes a task while its own `ttp checks` or push still runs (`waiting.hold_own_runs`, default on; an `urgent:` update still wakes), since they settle model-free when they end. A plain `needs_review` hand-off with a change to review (no follow-ups, notes or question) gets its review in its own finish step instead of at the 300 s sweep (`review.at_once`, default on). A routine review no longer waits for the checks the daemon started on its head: it runs beside them and reuses the recorded pass (`review.hold_for_precheck` true brings the hold back). The members of a batch whose lead ended `pushing` wait for its push result instead of being requeued alone and started at once.
