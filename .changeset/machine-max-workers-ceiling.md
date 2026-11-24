---
"tt-project": patch
---

tt-project: `machine.max_workers` is a supported, account-wide ceiling on worker runs of all projects on the machine together, held beside the load-based limit; unset means no ceiling, a bad value is a config problem, and an upgrade moves an old project's value to the account settings instead of dropping it.
