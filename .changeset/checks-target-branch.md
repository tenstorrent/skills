---
"tt-project": patch
---

`ttp checks` judges a head against the branch it lands on: a task's own `pr_branch` while `delivery.own_push` is not off, else `delivery.push_branch`. The new `--target <branch>` option names another branch (or any commit git can resolve); its `if_changed` scopes, fast path and push scan all use that one base. The failing-test rerun on the base was already taken from the task's fork point or stacked branch.
