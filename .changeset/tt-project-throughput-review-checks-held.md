---
"tt-project": patch
---

`tt-project`:

- `ttp throughput` no longer proposes lowering `coordinator.review_stall_s` or `review.sweep_after_s` for review waits the checks held: when checkcap's waits log for the code task (its own checks, or the precheck the daemon starts as it queues the review) and the review's precheck hold cover half the wait, the wait is still measured but judges neither limit. Waits the checks did not hold still lower them as before. The re-queue lanes no longer read a `checks_slot_wait` event kind that nothing writes; checks-slot waits come from checkcap's waits log alone.
