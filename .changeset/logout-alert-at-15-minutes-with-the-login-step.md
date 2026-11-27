---
"tt-project": patch
---

A logged-out provider that ready work waits on now alerts the user 15 minutes into the hold (the first logout reminder moved from 1 h to 15 min; the claim file still keeps it to one alert per hold across restarts), with the provider's re-login step. `ttp status` and the web app's status line show the same step beside "held: logged out".
