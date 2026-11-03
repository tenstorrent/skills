---
"tt-project": patch
---

`ttp secret env` from a sandboxed worker: the queued request carries its own start deadline (its 90 s wait plus a 30 s margin), so a caller killed by SIGKILL never has its command started late; the daemon records it lost instead. A second Ctrl-C during the withdrawal no longer cuts off the "nothing ran" report.
