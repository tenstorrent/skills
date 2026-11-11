---
"tt-project": patch
---

`tt-project`:

- a scheduled run (the daily review) that starts 2 h or more after it was queued re-reads the stats its spec carries (coordinator turns and spend, unblocking quality, Jev uses, host load and the rest) when it starts, so a review held up for a day no longer reports the day-old figures as today's. The stats are marked with when they were read; a coordinator's update after them stays. Coordinator turns already counted persistent-session wakes; tests.
