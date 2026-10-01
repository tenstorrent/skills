---
"tt-project": patch
---

`tt-project`: `ttp push` without `delivery.push_checks` now pushes changes that touch only docs and
refuses any other change before the rebase, naming the files and the key to set; the coordinator sets
the checks itself. With `delivery.base_ref` unset, code tasks branch from `delivery.push_branch`, then a
branch the charter names, then `origin/HEAD`, then the checked-out branch. Workers may run `ttp push`
in the foreground past the 5-minute rule, a wake refused for limits or auth keeps its waiting hand-off,
and a new memory entry never reuses an archived entry's name.
