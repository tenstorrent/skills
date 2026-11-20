---
"tt-project": patch
---

`ttp push --own` from a task worktree of another repository than the push branch's compares with that repository's own default branch when its remote has no push branch, instead of failing with "no push target to compare with". Required checks stay required: with none configured, a code change is refused and the message names the `delivery.push_checks_by_repo` key for that repository.
