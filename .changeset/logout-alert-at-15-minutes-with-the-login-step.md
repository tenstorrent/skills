---
"tt-project": patch
---

A logged-out provider that ready work waits on now alerts the user 15 minutes into the hold (the first logout reminder moved from 1 h to 15 min; once per project and hold across restarts), with the provider's re-login step. `ttp status` and the web app's status line show the same step beside "held: logged out". Every project whose own work waits on the login gets the reminder in its own chat, not only the project that holds the machine-wide claim.
