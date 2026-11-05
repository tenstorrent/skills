---
"tt-project": patch
---

`tt-project`: a `ttp checks` command that waits on an outside CPU pool (a test gate waiting for a pool slot, say) can give its host-wide checks slot up while it waits: it runs `ttp checks-yield` before the wait and `ttp checks-yield --resume` before its CPU work, which returns once the run holds a slot again, queued in the normal priority order (urgent first). Checks queued behind it start meanwhile. A waiter that dies while the slot is given up leaves the run to take one again for its further work, the next command of the run never starts without one, and a killed run holds none.
