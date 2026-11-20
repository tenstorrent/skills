---
"tt-project": patch
---

Checks spill: the account test pool also skips a host where a process outside the pool holds a Tenstorrent device node open (a device test that takes no lock). The host's ssh probe scans /proc/<pid>/fd without sudo, within its timeout; the skip names the pid and command. A scan that times out or cannot run keeps no run off the host and is told.
