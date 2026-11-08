---
"tt-project": patch
---

`tt-project`: spilled checks now spread across the spill hosts. Before, every new run tried the first listed host, so the other hosts ran nothing. Now a host already running fewer of this machine's checks is tried first. `ttp checks status` shows the project's last spill pick and why each skipped host took no run. A checks run parked here re-executes when the runtime is upgraded, and it then tries every spill host again, including hosts its checks failed on under the old runtime. Before, those hosts stayed off until the run ended. When a spill wrapper (`checks.spill_wrap_cmd`) puts its own environment first on `PATH`, the pinned checks environment goes back to the front, so the wrapper's environment can no longer hide it.
