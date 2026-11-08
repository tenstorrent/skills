---
"tt-project": patch
---

A done harness task whose named commits are not in the harness HEAD and that has no worktree of its own is no longer left stranded. Commits on a branch of its own, `ttp/t<id>-<slug>` (a worker that committed in another worktree of the harness repo), are merged into the harness checkout the same way `ttp/harness-t<id>` lands; a conflict is reported and tried again as before. A commit on no such branch raises one `harness_commit_stranded` event per task naming the commit and the branches holding it, so the coordinator can queue a land. Each done harness task is checked within two days of ending and settled once nothing is left to merge.
