---
"tt-project": patch
---

`tt-project`: the GLM guard no longer reads device or service words inside quoted text as a device step: a `git commit -m` / `-F` message or heredoc body, a `grep` or `rg` pattern, `echo` or `printf` text, or a `ttp advise --plan` brief passes. A real reset, power cycle, flash, firmware or service command is still refused, including through `sh -c`, `eval`, a script it writes and runs, or text piped into a shell. A run may always write scratch files in its own run folder; writes there to the run's own records (`run.json`, `glm_run.json`, `glm_guard.*`, `refusals.jsonl`, `glm_refusals.jsonl`) stay refused and hand back. On a GLM device task, a direct read of `/sys/class/tenstorrent` or `/dev/tenstorrent*` attribute files runs only as a step of Opus's device plan; with no plan it is out of plan and hands back, from Bash and from the Read and Grep tools alike, in both the hook and the guarded runner. Listing those folders (`ls`, `stat`) stays allowed.
