---
"tt-project": patch
---

A new coordinator session's seed cap prices each past new-session wake's seed at that wake's own
booked rate (its cost times the share of its weighted tokens that are cache writes and uncached
input, a 1h cache write weighing 2x input), not the project-wide rate, which averages mostly cheaper
worker runs and weighs cache writes at the 5m price. A past seed counts at most three times that
wake's own prompt size, so wakes that spend their whole raised cap no longer push the next cap up.
On a live project the cap went from $1.35 to $1.84 against a $1.20 seed that failed a $1.00 cap.
