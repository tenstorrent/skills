---
"tt-project": patch
---

`tt-project`: a command schedule's run status counts its `"error": true` lines apart from observations. A run that printed only error lines (the check could not run, including a failed exit code with no output) reads `error (N lines: check could not run)` and shows as failing in `ttp schedules`, `ttp status` and the web app, instead of `ok (N observations)`; a mixed run reads `ok (N observations, M errors)`. Exit 0 and 1 stay normal runs.
