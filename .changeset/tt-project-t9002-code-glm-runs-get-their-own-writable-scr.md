---
"tt-project": patch
---

`tt-project`:

- glmguard: every run under the GLM guards gets a scratch folder of its own as TMPDIR (TMP, TEMP), the guard allows writes there and its prompt says so; /tmp stays refused
- slop: --diff and the scans read the git repo holding the cwd (a worktree outside the project root) unless --repo names one; else the project root
