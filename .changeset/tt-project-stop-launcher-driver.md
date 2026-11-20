---
"tt-project": patch
---

`tt-project`: `ttp detach --stop NAME` with no detached job recorded finds a driver a project's own launcher started (NAME.pid in the path's folder, `$TTP_RUN_DIR` or the task's t<id> folder), verifies its pid, writes the STOP file beside it and escalates to SIGTERM and SIGKILL; a stale or reused pid reports already stopped, and nothing found names where it looked.
