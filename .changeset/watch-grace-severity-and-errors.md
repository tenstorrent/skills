---
"tt-project": patch
---

`tt-project`: a command schedule's `grace_min` holds only keyed lines at `normal` or above; keyed `low` and `info` lines (a routine "in sync") pass through as rated, never held or raised to `normal`. Could-not-run error lines now share one grace clock: inside it the run reads `retrying` and does not count toward the schedule's failure, so a short network drop no longer fails the schedule; past it they fail it as before. A run that could not run keeps the keyed clocks.
