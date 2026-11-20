---
"tt-project": patch
---

`tt-project`: `ttp checks` runs local checks in a fresh detached checkout of the commit it records (removed after a pass, failure, timeout or stop; ones a killed run left are pruned), so ignored, untracked or uncommitted files in the authoring worktree can no longer give a false pass. Submodules come from the authoring worktree's copies without network, or the checks fail clearly. A failure names files present only in the authoring worktree; uncommitted changes are named as not checked, with the commit that was. A check with `"cwd": "authoring"` opts out and its pass is labelled as not from a clean tree.
