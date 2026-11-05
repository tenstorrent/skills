---
"tt-project": patch
---

`tt-project`: the account test pool's device gate now probes each of a host's device locks under its own name as well as the lock this project's `ttp lock` takes for it. When `device.locks` lists another name first, both this project's own device job on the host and one another project runs under the shared `<alias>-device` lock keep that host off the pool; another project's hold of only the other name, which this project does not share, does not.
