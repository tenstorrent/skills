---
"tt-project": patch
---

`tt-project`:

- `ttp throughput` never proposes a `coordinator.review_stall_s` change: a sooner stall alarm starts no review sooner, it only adds coordinator wakes. The review_stall lane still reports its waits, and those the checks did not hold, as information only. `--undo` still restores an earlier change of it.
- `ttp throughput` no longer proposes lowering `review.sweep_after_s` for review waits the checks held: when checkcap's waits log for the code task (its own checks, or the precheck the daemon starts as it queues the review) covers half the wait, it is still measured but does not judge the sweep. A `checks_slot_wait` event the daemon raised for the code task inside the wait also counts as the checks holding it.
