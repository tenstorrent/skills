---
"tt-project": patch
---

`tt-project`: a lab host with no GitHub login can publish one of its own branches through the workstation gh route (`ttp push --publish <branch>`: a git bundle the workstation pushes as a new branch or a fast-forward, never the default branch, main or master) and retire one it pushed (`ttp push --retire <branch>`: refused while an open PR, task or worktree uses it; the tip is saved as a private ref and a bundle first). Prefixes beyond `ttp/` need a grant on both sides (`delivery.gh_branch_prefixes`, and the box's `gh_branch_prefixes` in the workstation's bridge config).
