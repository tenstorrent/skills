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

A routine checks run now leaves freed test slots to an urgent run of its own project parked for them,
whatever their arrival order, unless that run is itself held by a longer-parked run of the project or
left free room untaken for 5 min; `pool_gate` names the reserve. An urgent run whose preemption ask
lapsed or was dropped may ask again `checks.preempt_after_s` later (one live ask at a time, never while
its earlier request lives), so one wasted ask no longer ends preemption for its whole wait.
