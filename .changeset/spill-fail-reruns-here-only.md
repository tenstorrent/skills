---
"tt-project": patch
---

`tt-project`: checks that fail tests on a spill host (no host fault, timeout or env fault) run the failing tests once more on this machine only, whose result counts: they never go on from spill host to spill host. A host's own fault (a wrapper refusal, an unreachable host) still moves them to another free spill host. A workstation keeps its verdict after two failing spill hosts.

The tests gate's settings (`checks.judge`, `checks.quarantine_days`, `checks.judge_max_ids`, `checks.judge_rerun_s`, `checks.judge_busy_tries`, `checks.judge_busy_wait_s`) are known config keys, so a project can set them; before, each was refused as unknown.
