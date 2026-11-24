---
"tt-project": patch
---

GLM guard replay of off-spec deploy, push and box steps: a run in its own worktree can no longer write into its project's live harness or state checkout through inline code (a python copy or replace), `ttp pause` is refused on the GLM arm, `scp`/`rsync` to another host is caught even with a `2>/dev/null` redirect, and a read-only device task refuses a script fed to a remote shell over ssh (`ssh box "bash -s" < script`). A device tool hidden in a backquoted ssh heredoc, or in a heredoc or `echo` piped into `ssh box bash`, is refused in a plan step and at run time. Each audited case is now a replay test.
