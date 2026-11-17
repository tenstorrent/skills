---
"tt-project": patch
---

`tt-project`: post-landing checks are never cut short silently, and model scores publish again (t8742).
- remote checks: a spill wrapper that exits before it starts the check (for example a `set -e` failure in the wrapper's own setup) is a host fault that respills or reruns, never read as the check's exit code
- post-landing checks: a run that exits non-zero without a complete list of failing tests is rerun once; a pass then is a pass, and a second cut-short run is reported as interrupted, never as a test failure, with no revert or fix task
- daemon: the model-score refresh reads the project on a database connection made in its own thread, so scores publish again instead of failing with a SQLite thread error
- tests: the tree-kill timeout test waits on the child's pid being written and reads a vanished child's /proc state as gone, so it no longer races on a loaded host
