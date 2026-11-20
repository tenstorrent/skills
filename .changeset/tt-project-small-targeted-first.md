---
"tt-project": patch
---

`tt-project`: with the push queue on, a task's own checks stay targeted when the diff reaches half the suite or more (the push batch runs the full suite anyway); conftest, runner, manifest and config changes still run in full. A targeted run of fewer than `checks.small_tests` tests (account-level, default 500, 0 off) goes first among its project's waiters at its priority and is no longer held behind parked full runs of its priority or a less urgent one; the host cap, landings and every other hold still apply.
