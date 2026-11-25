---
"tt-project": patch
---

`tt-project`: GLM runs may pass `ssh`/`scp`/`sftp` `-o` options that run nothing, and `-F` with a settings file inside their own worktree or run folder that runs no local program, in a single plain call; specs that commit in the harness tree keep their run on the core provider; and the glm_work judge counts a task that bounced to the core provider only for its bounce, while a core retry of GLM's own failed work still counts against GLM.
