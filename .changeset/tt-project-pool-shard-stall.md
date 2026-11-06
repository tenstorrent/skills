---
"tt-project": patch
---

`ttp pool-shard` runs one shard of a gate's own test pool and ends it when no file is done within its stall window (four times the measured median seconds per file, at least 600 s; `checks.pool_shard_stall_s` / `checks.pool_shard_stall_floor_s`). A stalled shard's process group is ended, its batch job cancelled (`--cancel`), the stall logged with the shard id and pool alias, and its files re-run on the next pool given. The stalled pool is skipped by later shards for 30 min or until a file is done there. Time before a shard's first file (a queued batch job) is shown in the push log as a test-pool wait and, like any test-pool wait, counts against `delivery.push_checks_timeout_s`.
