---
"tt-project": patch
---

A detached job (`ttp detach`, local or `--remote`) whose command exits 0 but whose log's last test summary reports failures or errors (pytest's `N failed`, unittest's `FAILED (failures=N)`), as when a test command is piped into `tail`, is now recorded as exit 1 with a line in its log saying why, so an `on_pass` or `ttp detach --check` never acts on failed tests. Failing exit codes, pipelines cut by `| head` and `ttp` commands keep their own exit code; `TTP_DETACH_RC_ONLY=1` opts a job out.
