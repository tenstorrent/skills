---
"tt-project": patch
---

Batch runs keep apart work that cannot be delivered together. A task with a `no_push:` line is a delivery route of its own, like a `pr_branch:` one: it neither leads nor joins a batch, so push-queue work never shares a run with work kept off the push branch. An urgent lead now takes only urgent tasks that already wait on it or its other members and serve the same user orders; independent urgent tasks, and tasks of another order, start their own runs in parallel. Routine batching is unchanged.
