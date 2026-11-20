---
"tt-project": patch
---

`tt-project`: a detached `ttp checks` run that can take `delivery.task_checks` (a candidate run of a single-repo project's own checks, not `--release`) records the task set's id, so the daemon stops it as superseded when `delivery.task_checks` changes and leaves it running when only `delivery.push_checks` changes. Runs on the push checks are compared against the push checks as before.
