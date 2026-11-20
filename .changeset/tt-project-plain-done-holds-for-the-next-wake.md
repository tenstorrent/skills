---
"tt-project": patch
---

A coordinator batch of only plain hand-offs (a normal task_done of no review, with no follow-up proposed, no waiting parent, no live dependent and no text asking for a decision or naming a Restriction) no longer wakes a turn of its own once `coordinator.batch_s` passed. It joins the next wake of any other cause (a user message, a failure, any other event) or the idle wake, at the latest `coordinator.plain_done_hold_max_s` (default 3600 s, the idle wake default; 0: off) after its oldest event. The events stay queued, so a daemon restart keeps them and their clock. Both coordinator modes and every provider share the rule.
