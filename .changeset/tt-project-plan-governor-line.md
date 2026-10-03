---
"tt-project": patch
---

On a subscription plan the governor no longer spreads use evenly over each window. Below the
line (100 - reserve_pct) every worker slot runs. Near the line it counts what running work may
still add (measured burn per worker times a typical run length, capped at the reset), runs only
as many workers as fit, and starts nothing once running work alone would reach the line. At the
line nothing starts until the reset. Pace holds and `budget.max_pace_hold_s` are removed, and
user tasks, chat replies and reviews no longer bypass a hold near the line. All slots being busy
far below the line is no longer shown as an orange "no new starts" hold.
