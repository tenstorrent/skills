---
"tt-project": patch
---

`tt-project`: a `ttp checks` run also waits outside the host queue, keeping its arrival, while the host's load is above `checks.load_guard` or fewer than `checks.test_run_min` test slots are free. A wait bounded by `delivery.checks_spill_after_s` no longer takes and hands back a host-wide checks slot at every bound. A priority-1 run whose project's test budget is held only by less urgent runs still counts its urgent slots as room.
