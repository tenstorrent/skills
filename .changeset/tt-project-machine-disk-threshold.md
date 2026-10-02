---
"tt-project": patch
---

`tt-project`: a machines-list entry may set its own disk guard threshold (`ttp machines add <alias>
--min-free-gb N`, `--hostname H` when the alias is not the short host name). On that machine it
replaces the projects' `disk.min_free_gb` (0 turns the guard off there; "" goes back to the
projects' own), so a shared disk that other services keep near full does not hold every project.
The disk guard alert, and the coordinator's digest while low, now say how much of the used space is
the project's own data and name the biggest top-level directories (`du -x -d 1`, run once per
low-disk episode and stopped after 30 s, keeping what it measured).
