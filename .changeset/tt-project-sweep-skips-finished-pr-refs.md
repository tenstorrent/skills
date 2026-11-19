---
"tt-project": patch
---

`tt-project`: the blocker sweep leaves out done, failed and cancelled tasks (also ones that finished after a sweep listed them), drops a sweep outcome that names one and closes any outcome left on one, so a finished task is never recorded still blocked or given a probe check. Wait notes and landing waits no longer read a PR or issue number as a task (`PR: #<n>`, `the PR (#<n>)`, `PRs #<n> and #<m>`, `pull request #<n>`, `org/repo #<n>`); `task #<n>`, a bare `#<n>` and `landed:#<n>` still name tasks.
