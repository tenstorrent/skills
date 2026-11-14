---
"tt-project": patch
---

`tt-project`: the push audit counts a done task's claimed push as landed when its head is reachable from any remote-tracking ref other than the claimed branch's own, so a cancelled own-branch push whose work landed another way is no longer re-raised. `ttp task <project> superseded <id> <sha> --ref <target>` accepts the task's own head when the target holds it; older commits of the branch are still refused as predating the work.
