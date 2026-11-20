---
"tt-project": patch
---

`tt-project`: a task whose wait is over no longer queues for hours behind new work while every worker slot is taken. Within one priority, dispatch now picks a woken wait (its `retry_when` passed, or its checks, job or `on_pass_cmd` woke it) before any other queued task, the one ready longest first. Priority still wins across levels: a p1 or p2 new task still goes before a p3 resume. A passed wait whose `on_pass` or `on_pass_cmd` (such as the filled-in `ttp checks --settle`) settles it is still recorded by the daemon itself, with no worker slot and no model run; a test now covers this with a full worker cap. New starvation guard: a woken wait not started `coordinator.resume_wait_min` minutes (default 15; 0 turns it off) after it became ready raises one `resume_wait` observation naming the task and what holds it. Spend caps, the budget gate's worker cap, locks, device holds, pauses and required checks are unchanged.
