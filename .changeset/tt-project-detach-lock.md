---
"tt-project": patch
---

`tt-project`:

- `ttp detach <name> --lock <resource>` (repeatable, or `A,B`): the job runs its command under one `ttp lock` of them all, so it takes them all or nothing by ttp lock's rules before the command starts and holds them until the command ends (its `.rc` follows) or its processes are gone (a job found lost). A paused one exits 75 at once and starts nothing. A lock taken inside a detached job names the job, its `.rc` and its pid in its holder label (`ttp lock --probe --json`, holder notes). Without `--lock`, `ttp detach` is unchanged.
