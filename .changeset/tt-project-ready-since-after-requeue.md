---
"tt-project": patch
---

`tt-project`: the ready-not-started check counts a task's wait from when it re-entered the queue, not from its last run's end. A review the push queue sends back after hours in `pushing` no longer raises a false "ready 171 min, held by nothing" alert seconds before dispatch starts it: its ready time includes the push queue's last move of its head, and a task the previous check did not find ready (a passed wait probe, a requeue) counts from that check.
