---
"tt-project": patch
---

`tt-project`: a worker's system prompt is smaller (about 5.8 kB less for a large charter). The charter's Goals and Resources leave out untagged blocks that name the coordinator or the daily review and no worker; a `[workers]` or `[coordinator]` tag still decides, and the header counts the left-out items. Shared-cluster rules turn on only when the charter's Resources or Restrictions, or the task itself, name a cluster, Slurm or a slurm machine, not when a goal mentions one. Some worker.md rules a hook already enforces are shorter. Restrictions and user corrections stay verbatim.
