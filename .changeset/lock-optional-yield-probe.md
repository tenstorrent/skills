---
"tt-project": patch
---

`ttp lock` can now yield a busy resource mid-command: config `locks.yield_probe` names a quick,
read-only shell command that the runtime asks every `locks.yield_every_s` (15) s while the locked
command runs, with the lock names in `$TTP_LOCK_NAMES`. Exit 0 stops the command (SIGTERM to its
process group, SIGKILL after 10 s), releases the lock and exits 75, so the task hands itself back as
waiting; exit 1 keeps it running, and any other answer is reported once and never stops it.
`locks.yield_exempt` (globs of lock names) and `--no-yield` opt a call out, for rollout and heal
takes. Off by default: unset, nothing runs.
