---
"tt-project": patch
---

`tt-project`: a coordinator batch that records a user order (`obligation` with `order`) now wakes, at once and at their own tier as `wake_now` does, the tasks asleep on a wait whose steps it changes: a `task_update` `spec` in the same batch, the order's owners, or a task still carrying a step it supersedes. One wake per order and task; it skips the 30-minute coalesce window and the same-wait spec hold, while the hourly cap and every hold (paused project or resource, locks, provider, budget) still apply. A spec note without an order still sleeps until its wait ends.
