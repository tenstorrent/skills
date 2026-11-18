---
"tt-project": patch
---

The GLM code arm's shell guard reads a `ttp` call without its redirections (`ttp checks 2>&1 | tail` is `ttp checks`, not `ttp checks 2`), allows `ttp paths`, and lets a command read the run's identity (`echo TTP_RUN_DIR=$TTP_RUN_DIR`, `printenv`, `grep TTP_TASK=`) while still refusing every edit of it: an assignment where the shell reads one (at a command's start, after `;`, `&&`, `(`, `$(`, `then`, `sudo`, `env`, `bash -c` or `eval`), `export`, `declare`, `unset`, `read`, `printf -v` and `env -i`.

The policy-file check after a GLM code-arm run reads the task's diff in the task's own code repository (its worktree, or the repository that worktree belongs to) instead of only the project root, and keeps the evidence ref and resets the branch there. A branch or base it cannot read is a high `policy_diff_unread` event and sends the task back to Opus, never a silent pass.
