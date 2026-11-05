---
"tt-project": patch
---

`tt-project`: the account test pool's device gate now probes each of a host's device locks under its own name as well as the lock this project's `ttp lock` takes for it. When `device.locks` lists another name first, a device job another project runs under the shared `<alias>-device` lock keeps that host off the pool, and a hold on the other name keeps it off only when that name is the host's too (`device.hosts` or `device.runners`).
