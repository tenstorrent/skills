---
"tt-project": patch
---

`ttp push --own` on a commit where every project check is skipped as not applicable now runs the extra commands `ttp checks -- <cmd>` recorded passing on exactly that commit (they must pass again) instead of refusing. A record of another commit, a failed record or one with only project checks still refuses, and the refusal says how to fix it: record a check with `ttp checks -- <cmd>` or add a delivery check that applies. `ttp push --detach` carries the record into the push process.
