---
"tt-project": patch
---

`tt-project`:

- setup-box Slurm route: build once and test in parallel, resumable case jobs, model-free job verdict
- ttp detach: keep a job's TMPDIR while a setsid child that outlived it still holds its lock; sweep drops it once free
- smoke list: add a slurm verdict case
