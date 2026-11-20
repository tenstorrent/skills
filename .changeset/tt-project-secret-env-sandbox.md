---
"tt-project": patch
---

`ttp secret env` works from a sandboxed worker: the command is queued for the daemon, which runs it outside the sandbox with the secrets in its environment and hands back its masked output and exit code; the value never enters the sandbox, and `state/secret-uses.jsonl` records only the secret names, the task and the time. `--secret <builtin>:<field>=VAR` (e.g. `jev:key=VAR`) gives a built-in secret's field; the plain `jev=VAR` form is still refused. The run folder's job list shows the routed job as the worker gave it (`ttp secret env --secret=... -- <command>`), not the launcher's long PYTHONPATH prefix.
