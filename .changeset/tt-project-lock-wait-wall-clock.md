---
"tt-project": patch
---

`tt-project`: workers that share a device no longer time out, lose results or pause the project
while they queue for it.

- Time a run spends waiting in `ttp lock` no longer counts against its wall clock, up to the
  wall clock once more. Overlapping waits of one run count once.
- A run that times out after writing its hand-off keeps its result, and does not count as waste.
- At most twice a resource's slots run at once among the tasks that use it; the other worker slots
  go to ready work that does not need it. Held tasks stay queued.
- The runaway guard's waste limit (`budget.hourly_waste_usd`) is now per active worker, counted as
  the most tasks whose failed or stalled runs of the last hour went at once, up to
  `budget.max_parallel_workers`.
