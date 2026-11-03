---
"tt-project": patch
---

`tt-project`: the blocker sweep leaves out done, failed and cancelled tasks (also ones that finished after a sweep listed them), drops a sweep outcome that names one and closes any outcome left on one, so a finished task is never recorded still blocked or given a probe check. Wait notes and landing waits no longer read a PR or issue number as a task (`PR: #8`, `the PR (#8)`, `PRs #8 and #16`, `pull request #8`, `org/repo #8`); `task #8`, a bare `#8` and `landed:#8` still name tasks.
