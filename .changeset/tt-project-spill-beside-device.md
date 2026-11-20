---
"tt-project": patch
---

Checks spill: a project can list hosts in `checks.spill_beside_device` (ssh aliases, or `"*"`) where its CPU-only checks may run beside a device job. On a listed host a device lock, broker job or device holder no longer keeps a spilled run off; a pause or a held machine lock still does, and the run and its `ttp status` line say it runs beside a device job. Unset, nothing changes.
