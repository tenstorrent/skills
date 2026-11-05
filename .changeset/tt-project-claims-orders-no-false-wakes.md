---
"tt-project": patch
---

`tt-project`: claim and order records no longer raise false must-act wakes. A claim's check probe runs under its own cache key (claim and due), so an earlier run of the same probe by a probe-stall re-check or another claim, from before the claim's due, is never taken as its answer. Claims leave the open-promise list (the promises digest, the announced-not-started check, the sweep and expected-finish lines) and the long-promise check: the claim check alone judges them. An order that has not started gets one start-late signal, its `order_late`, and no `announced_not_started` on top.
