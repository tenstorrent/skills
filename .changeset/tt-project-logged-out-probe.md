---
"tt-project": patch
---

`tt-project`: While a provider is logged out, the daemon starts at most one run on it per 15 minutes to
check the login (the coordinator if it has work, else the cheapest queued task) and keeps every other task
queued without using an attempt. `ttp status` and the web app show those tasks as "held: logged out".
The logout alert clears, and all queued work starts on the same tick, once a run succeeds or a probe is
clearly past the login (it spends tokens or is still running after 2 minutes), so a working probe no longer
holds the coordinator and the queue for its whole run.
