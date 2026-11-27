---
"tt-project": patch
---

`ttp checks` judges a head against the branch it lands on: while `delivery.own_push` is not off, the `pr_branch` of the task, or of the task its fix or review covers, else `delivery.push_branch`. The new `--target <branch>` option names another branch (or any commit git can resolve) and is kept when the daemon starts queued checks; one that names nothing here is refused. Its `if_changed` scopes, fast path and push scan all use that one base. The failing-test rerun on the base was already taken from the task's fork point or stacked branch.
