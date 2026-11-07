---
"tt-project": patch
---

Checks preemption hands the freed slot over at once: an urgent run parked for its test slots keeps its
claim on the slot its preempt frees while the asked run still holds it, looks every half second while
that claim lives, and starts within a second of the slot freeing instead of at its next 5 s poll. A
parked asker that still cannot start 3 s after the slot freed drops its claim, so the next waiter takes
the slot instead of it sitting idle for the claim's 30 s. `ttp checks --slot-status` lists runs parked
outside the host queue under the waiters, never as holders, and no longer says "waiters: none" while
one is parked.
