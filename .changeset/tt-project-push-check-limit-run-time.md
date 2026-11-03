---
"tt-project": patch
---

`tt-project`: a push check's time limit (`delivery.push_checks_timeout_s`) now counts only the time it runs, not its waits for a grant (a `ttp lock`, a checks lane or a host-wide checks slot). A check still waiting for a grant once its limit plus the push wait have passed is ended before it ran: the push exits 75 ("queued, not run") with a `retry_when`, never a failed check. A check that runs past its limit still fails the push, and the message says how long it ran and how long it waited.
