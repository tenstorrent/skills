---
"tt-project": patch
---

`tt-project`: spend is booked when a run ended, not when the daemon got to it. A run the daemon
reaps after downtime books its cost at the time it ended, so the hourly runaway guard no longer
counts a run that ended hours ago as last-hour spend.
