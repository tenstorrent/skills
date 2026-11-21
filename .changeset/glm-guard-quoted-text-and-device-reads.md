---
"tt-project": patch
---

`tt-project`: the GLM guard no longer reads device or service words inside quoted text as a device step: a `git commit -m` / `-F` message or heredoc body, a `grep` or `rg` pattern, `echo` or `printf` text, or a `ttp advise --plan` brief passes. A real reset, power cycle, flash, firmware or service command is still refused, including through `sh -c`, `eval`, a script it writes and runs, or text piped into a shell. A run may always write scratch files in its own run folder; writes there to the run's own records (`run.json`, `glm_run.json`, `glm_guard.*`, `refusals.jsonl`, `glm_refusals.jsonl`) stay refused and hand back. On a GLM device task, a direct read of `/sys/class/tenstorrent` or `/dev/tenstorrent*` attribute files runs only as a step of Opus's device plan; with no plan it is out of plan and hands back, from Bash and from the Read and Grep tools alike, in both the hook and the guarded runner. Listing those folders (`ls`, `stat`) stays allowed, unless the listing feeds another command (a pipe, `$( )`, `<( )`), which can read what it names and counts as a read.

The GLM guard reads quoted text with the hook's one data-only rule, so a markdown table, a commit message, a `grep` pattern, `echo` text, python code in a quoted heredoc or a script written for later may name `$(...)`, backticks or `ttp push` freely. A program name or `sleep` time hidden in an expansion in the command words (`$S 2`, `eval "$x"`, `${!n}`) is a plain redirect with its allowed way; only the third refusal of a run hands it back. Text piped into a shell (`echo '...' | bash`, `|& dash`) is read as commands. A run may read a rollout with `ttp rollout status`; changing one stays refused.

Watchers: a stable-key line whose episode names the newest member of a changing set wakes once per new member, and an episode reported on more than one run before the next is no longer counted as churn.

why this size: it re-applies an earlier reverted guard change together with its speed fix, and most of the added lines are tests of the guard's decisions.
