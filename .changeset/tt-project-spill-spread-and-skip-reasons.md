---
"tt-project": patch
---

`tt-project`: spilled checks now spread across the spill hosts. Before, every new run tried the first listed host, so the other hosts ran nothing. Now a host already running fewer of this machine's checks is tried first. `ttp checks status` shows the project's last spill pick and why each skipped host took no run. A checks run parked here re-executes when the runtime is upgraded, and it then tries every spill host again, including hosts its checks failed on under the old runtime. Before, those hosts stayed off until the run ended. When a spill wrapper (`checks.spill_wrap_cmd`) puts its own environment first on `PATH`, the pinned checks environment goes back to the front, so the wrapper's environment can no longer hide it.

If spilled checks fail at import because a package is missing (for example `No module named ...` while tests are collected), the spill environment's config is at fault, not the host or the change. That failure no longer keeps the host off for the rest of the run. Once the same import fails twice, the run reports it once, waits here, and raises one high-severity event to the coordinator. It tries every spill host again when the `checks` config changes or after 15 minutes.
