---
"tt-project": patch
---

With `budget.daily_target_usd` set (unset by default: no line), the daily review's self-efficiency
audit (`ttp audit`) lists each project on this machine with the last 24 h's spend by class
(coordinator, reviews, worker runs that ended waiting, other workers) and the total against the
target. Real spend (a Claude run's own tokens priced at the model it actually used) is shown with the
booked figure beside it where they differ.
