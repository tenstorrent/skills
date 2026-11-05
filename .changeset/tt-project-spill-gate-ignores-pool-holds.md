---
"tt-project": patch
---

`tt-project`: the account test pool's device gate no longer reads a pool check run's own hold of a host's lock as a device job. When `device.locks` lists the bare machine alias, another project's (or this project's) check run on that host kept it off the pool for every project on the account; now only a hold without the check-run mark, a reservation, a waiter or a held `<alias>-device` lock does.
