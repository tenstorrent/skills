---
"tt-project": patch
---

`tt-project`: the pre-push leak scan no longer times out in a blob:none partial clone: its object lookups set `GIT_NO_LAZY_FETCH` (git 2.45+), and the blobs its `git log -p` diffs over the sent range are fetched from the promisor remote in one batch first. `checks.test_run_min` (project and account) and `delivery.push_hard_tests` are known settings, so setting them no longer prints an unknown-key hint, and an account-level `checks.test_run_min` now takes effect. The guide says which push-batch failures the tip's own failure excuses.
