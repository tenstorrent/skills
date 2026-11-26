---
"tt-project": patch
---

`tt-project`: no task or promise sits behind a gate that no longer truly blocks it. Once a minute, without a model call, a task queued on a machine that is held, lent out, in a recorded outage or failing repeatedly moves to another machine the project's config allows (a hold or pause is never lifted); a gate older than `coordinator.stale_gate_min` (default 30) that states no reason is cleared, and so is a dead dependency the task never names; and a promise whose owners cannot run gets its one clear replacement. Anything else becomes a `stale_gate-<task>` must-act item, and the Course check flags any task or promise held over the threshold.
