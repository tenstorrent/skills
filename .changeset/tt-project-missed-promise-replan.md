---
"tt-project": patch
---

`tt-project`: a missed promise changes the plan, never only its time. Each promise, order and claim records its misses and due moves. Past its due, an `obligation` due move is refused unless the same batch changes the plan on its critical path (a task_add naming it or continuing an owner, an owner's task_update of priority, resources, depends_on, status or spec, a new owner, a config_set on its bottleneck, or a `justify` naming what changed); a move citing the user's own message (`message`) always goes through. A reply giving a new time in a missed promise's chat with no plan change raises one `promise_moved_unchanged` event, a second miss makes the must-act item a re-plan, and the daily review shows a `missed promises` line (missed, missed twice or more, due moves without a plan change, median lateness). Clock times from 20:00 to 23:59 now count as promise times.
