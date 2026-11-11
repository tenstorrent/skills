---
"tt-project": patch
---

tt-project: one machine-wide limit on concurrent workers across all projects on a laptop or workstation (`machine.worker_limit`, auto: on for macOS or a machine with a battery, off on servers; `machine.max_workers`, unset: half the cores, 2 to 8). It is lowered while the CPU is busy, memory is short (free share, macOS memory pressure and compressor, Linux PSI), a call app holds the display awake, or the machine is hot (thermal pressure or zones), never below 1. Each project's `budget.max_parallel_workers` stays its own ceiling and is clipped to the machine limit. Projects share the count and the readings in `TTP_HOME/machine-workers.json` under a file lock, and each start claims its place, so starting all ready work in one tick stays under the limit. Off: `ttp config <name> machine.worker_limit off`.
