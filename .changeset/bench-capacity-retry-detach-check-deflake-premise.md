---
"tt-project": patch
---

`tt-project`: `python3 -m ttp.benchblind review ...` no longer crashes with a TypeError (main() read no argv in module form). The chat bridge waits out a model at capacity (HTTP 429) inside the run, honoring Retry-After or the body's "retry after N seconds" hint, else a backoff, within `providers.chat.busy_retries` (default 4) and `providers.chat.busy_wait_s` (default 180 s total); a bench cell that runs out is recorded as a capacity stop so a rerun picks it up. `ttp detach --check` of a path where no job was ever recorded says so and exits 2 without a `.lost` marker, and a bare job name resolves in this run, then in the run folders that recorded it. Each deflake task the tests gate files carries a read-only `premise_cmd: ttp quarantine still <id>`, so the daemon closes it before dispatch once none of its tests is quarantined any more or a landed fix resolves each.
