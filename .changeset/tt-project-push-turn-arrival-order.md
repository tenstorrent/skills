---
"tt-project": patch
---

tt-project 0.2.998: pushes waiting for a branch's push turn take it in arrival order (a ticket in the lock's queue) instead of polling the lock, so a later push never takes the turn from an earlier one. The wait gives up (exit 75) only when one holder keeps the lock past `delivery.push_wait_s`; it starts over each time the turn moves on, so a push queued behind many short ones is no longer sent away, and it ends after 4 h in all. `ttp push --free` no longer counts detached pushes that only queue for the turn, nor a holder still within the push wait (a push started then queues behind it), so a task asleep on it is not starved while pushes take turns. The push queue's batch start reads the same rule.
