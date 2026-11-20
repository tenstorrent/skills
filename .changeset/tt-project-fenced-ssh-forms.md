---
"tt-project": patch
---

`tt-project`: hook: a sandboxed Claude worker's ssh, scp, sftp or rsync line that the sandbox keeps fenced is refused before it runs, now also for a lone command with a redirect to or from a file (`<`, `<<`, `> file`), a `$(...)` or backtick anywhere in the line, ssh inside `bash -c` or `eval` not run by `ttp lock <res> --`, and a pipe after `ttp lock <res> -- bash -c '...ssh...'`. The refusal names the cause (only a simple command is freed; a refused 127.0.0.1 port is the sandbox's own loopback, where only forwarded host ports answer) and the workaround: a local wrapper script run as `ttp lock <res> -- bash wrapper.sh` (a plain `bash wrapper.sh` stays fenced, measured).
