---
"tt-project": patch
---

The worker hook refuses a fenced Bash line whose leading `VAR=value` assignment prefixes ssh, scp, sftp,
rsync or a project's fence-freed command: the sandbox frees no line that starts with an assignment, so
it would run fenced and fail to reach the host. The refusal says to pass the value as an option of the
command or set it inside the quoted remote command (`ssh host 'FOO=x cmd'`); the same lines without
the prefix pass as before.
