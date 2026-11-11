---
"tt-project": patch
---

`tt-project`:

- the overseer takes the time the host slept (a closed laptop lid, a system suspend) out of the daemon heartbeat's age: a heartbeat aged only by sleep opens no `loop heartbeat stale` incident and captures no stacks on wake, and the `loop health:` line says `host asleep` instead. A stall while the host is awake still raises, counted in awake time. The sleep comes from the kernel clocks (Linux BOOTTIME minus MONOTONIC, macOS MONOTONIC minus UPTIME_RAW) and is kept in the health file across overseer restarts; a reboot infers no sleep. Tests with faked clocks.
