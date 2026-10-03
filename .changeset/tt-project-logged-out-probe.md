---
"tt-project": patch
---

`tt-project`: While a provider is logged out, the daemon starts at most one run on it per 15 minutes to
check the login (the coordinator if it has work, else the cheapest queued task) and keeps every other task
queued without using an attempt. `ttp status` and the web app show those tasks as "held: logged out".
Once a run succeeds the logout alert clears and all queued work starts on the same tick.
