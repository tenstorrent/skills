---
"tt-project": patch
---

`tt-project`: a worker's Bash call that fails on Claude Code's sandbox mount race ("bwrap: Can't get type of source path ...: No such file or directory", or "Can't bind mount ...") now tells the worker it is a transient race and to re-run the same command unchanged. The hint comes from a new `PostToolUseFailure` hook, which also hands over coordinator updates and hand-off problems after a failed tool call (Claude Code sends no `PostToolUse` for one). `ttp audit` lists how many Claude runs in the window hit the race, of all Claude runs (`mount_races` in `--json`). The write fence is unchanged: it grants the same write roots as before.
