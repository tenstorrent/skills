---
"tt-project": patch
---

`tt-project`: a new local `ttp checks` run no longer starts while the host's disk is saturated by writes: `Dirty:` in /proc/meminfo over `checks.dirty_max_mb` (default 2048 MB) or /proc/pressure/io `full avg60` over `checks.io_full_max` (default 10%; skipped on kernels without PSI). A run that may spill tries the spill hosts first, else it waits in its place and rechecks without a model call; running checks and the push branch's checks are not held. Both are account settings (0 turns a signal off), and `ttp checks --slot-status` shows the measured values and the reason.
