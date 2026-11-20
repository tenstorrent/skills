---
"tt-project": patch
---

`tt-project`: a lock queue (`ttp lock`, `ttp checks` lanes and test slots) no longer keeps places for waiters that can no longer take them. Each queue entry now records its waiter's pid and start time and, outside a `ttp detach` job or detached `ttp checks` (which outlive their run by design), the run it waits for. Whoever reads the queue drops an entry whose waiter process is gone or whose pid now belongs to another process, even when a child the waiter left still has the queue file open, and an entry whose run has ended. The next live waiter then moves up. Live waiters, and entries this host cannot judge (another host or PID namespace, an older runtime's entry), keep their places and order. A waiter whose run ended never takes a turn: `ttp lock` exits 75 and says why, and a checks wait is cancelled. Before, entries left by ended runs could fill a queue's places for hours.
