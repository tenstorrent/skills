---
"tt-project": patch
---

`tt-project`: on plan windows, pacing scales the time-weighted mean of the project's workers over
the measured burn span instead of the count running now, so a burst after a reset no longer holds
the project at one worker. Pace rows show `avg_running` and `allowed`.
