---
"tt-project": patch
---

A model-free watch raises one `checks_host_overloaded` observation to each project's coordinator when
the host's 5-minute load stays above the load guard's threshold (its cores, or `checks.load_guard` x
its cores) for 15 min while local check runs sit at or near the checks cap and no spill host takes
any. It names the load, the runs, the cap and `delivery.checks_spill_hosts` (and lowering
`checks.host_max`), and closes once that no longer holds. `ttp checks --slot-status` and the daily
review show the check runs per host and this host's load.
