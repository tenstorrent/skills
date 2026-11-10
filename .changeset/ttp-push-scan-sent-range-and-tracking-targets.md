---
"tt-project": patch
---

The pre-push leak scan looks up only the remote tips it stops at (the updated refs' remote tips, the remote's HEAD and its branch tips, capped at 2000) instead of every advertised ref, so a remote with tens of thousands of refs no longer times the scan out; its timeout grows with the tip count and a real timeout still refuses the push. The push check target lint (missing target at the tip) reads the remote-tracking push branch, fetched first by `ttp doctor`, instead of the local branch that drifts behind; only a repo with no remote uses its local branch.
