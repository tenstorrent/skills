---
"tt-project": patch
---

An urgent checks run that waits in several host-wide queues (parked, then the checks queue, then the test pool's host line) raises one `urgent_check_wait` event in all, not one per queue, so it wakes the coordinator once.
