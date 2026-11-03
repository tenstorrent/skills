---
"tt-project": patch
---

Detached jobs: every `ttp detach` job (a worker's direct start, and the daemon's queued, relaunched and
heal fix starts) runs with no_new_privs, so nothing in it can gain privileges; a privileged step goes
through `ttp host-run`. `ttp host-run` stays unfenced; a daemon that inherited no_new_privs while
`runner.host_commands` allows commands now says so once at start and in `ttp doctor`, and answers each
host-run request with that plain reason (exit 126) instead of sudo's error; a `--detach` request starts no
supervisor and writes no rc file.
