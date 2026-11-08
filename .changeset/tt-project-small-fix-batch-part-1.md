---
"tt-project": patch
---

`ttp upgrade --apply` over uncommitted edits git refuses to overwrite exits 75 (busy, nothing changed) instead of 1, and the merge task reads 75 as retry later. A hand-off whose `retry_when` feeds a `detach --check` to `||` is stored guarded, so a failed job's exit 4 wakes the task instead of falling through to the fallback; worker.md says never to write one. `detach` reads a bare `<dir>/<name>` job path as `<name>.rc`, matches detached.json entries by absolute path, and lets an exit code written while a loss is judged win, so an ended job is no longer marked lost. A claim on a superseded review holds through the review that covers it, and superseded tasks count as closed. Claude Haiku 5.5 calls over 100K prompt tokens are booked at the 5x long-prompt rate, per call and on a run's summed tokens.
