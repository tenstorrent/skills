---
"tt-project": patch
---

`tt-project`: a user-ordered urgent task (a fresh `urgent:` line) starts within minutes even when every worker is busy. It may take two burst slots past the cap (`budget.urgent_extra_slots`, was 1) at a green, yellow or orange gate, including an orange plan gate of 0 slots, never at red or past the dollar caps. With those full too, dispatch stops the lowest-priority routine run at a safe point and queues it again with its work kept and no attempt spent (`budget.urgent_preempt`, on by default); urgent work, reviews, pushes, harness tasks and runs holding a lock are never stopped. Its `ttp lock` and `ttp checks` waits go ahead of other urgent waiters. One that names a machine or device resource and is still queued, or still waiting in `ttp lock`, after `coordinator.urgent_start_wait_min` (5) raises one high event naming what holds it or the lock's holders. `budget.urgent_preempt` set from chat to "false" now turns it off.
