---
"tt-project": patch
---

`tt-project`: the GLM run guard no longer lets a run write through a system temp entry it made that is a link, or holds a link, to another temp entry: a write passes only when the entry itself is no link and the path resolves inside it (before, anything still resolving under `/tmp` passed, and the guard's own test failed when pytest's basetemp sat under `/tmp`). pytest's summary reader (`sharedchecks.pytest_summary`) now reads only count labels ending in a pytest outcome (passed, failed, error(s), skipped, deselected, xfailed, xpassed, warning(s), rerun), so `158 subtests passed` still counts but another tool's line printed after pytest's, such as `12 packages installed in 3.1s`, is no summary. Before, such a line hid the real counts from the tests gate, lost-worker accounting (which could take real failures for a lost worker alone) and output trimming.
