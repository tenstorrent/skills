---
"tt-project": patch
---

A GLM code-arm run can run its own project's checks again in every form (`ttp checks`, `--dry-run`, `--result`, commands after `--`, after `cd <worktree> &&`, piped or redirected). The guard read the command's name from `a.cmd`, which `ttp checks` uses for the commands after `--`, so it saw an empty name and bounced the run. The CLI now reads the name from argv, for the guard and the on_pass_cmd dry-run stub, and a refusal always names the command it refused.
