---
"tt-project": patch
---

`tt-project`: a `ttp checks` run waiting for a lane no longer forks `ps` for every pid it checks. On Linux, process start times come from /proc only, and a pid missing there counts as gone. Before, every finished run's dead pid in the checks-join records cost one `ps -o lstart=` per poll. On hosts without /proc (macOS), one `ps -A` per poll cycle answers every pid's start time and zombie state. The start time is still compared, so a reused pid is still told apart. A waiter at the head of the queue now polls every 1 s instead of 0.5 s. Measured on a busy host with one waiting run and 30 finished-run records over 30 s: process creations went from 360 to 0 and CPU from 80% of a core to 0.4%. On the `ps` path it is one `ps` per second.
