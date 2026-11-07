---
"tt-project": patch
---

A coordinator `task_update` spec can now edit the live spec instead of only appending to it. A line `retire: "<exact earlier text>"` drops every copy of that text, and a first line `replace:` makes the rest of the update the whole spec, with the earlier spec's Restrictions and guard lines kept. The earlier text is kept as a `spec_step_replaced` event, and a running worker's update says plainly that it no longer applies. A quote that matches nothing, is under 8 characters or touches a safety line is refused back to the coordinator. A spec without these exact lines is appended as before. The task_update row of the coordinator prompt documents both lines.
