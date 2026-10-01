---
"tt-project": patch
---

`tt-project`: 0.2.29 makes a new-task cap of 0 stop new tasks again (it had come to mean no cap),
clamps `coordinator.max_new_tasks_per_day` to 0..1000, and never wakes the coordinator in a loop
while the cap is 0; raising the cap wakes it at once. Review tasks get their own rolling 24 h cap,
`coordinator.max_review_tasks_per_day` (default twice the new-task cap, at most 2000), so a
runaway turn cannot add reviews without limit.
