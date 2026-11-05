---
"tt-project": patch
---

An urgent check run (priority 0 or 1) never waits behind routine ones with every host-wide checks slot busy: it tries a free host of the account test pool at once, then a reserved urgent slot beyond the cap (`checks.urgent_slots`, default 1, 0 turns it off), then asks the lowest-priority routine run on a cap slot to step back; that run ends its command and queues again at its old place, keeping what it recorded.
