---
"tt-project": patch
---

An ended task's own leftover processes no longer hold a checks slot or lane forever. When they are the only holders, the daemon ends them (SIGTERM, then SIGKILL) and writes a `leftover_procs_stopped` receipt to the feed. Attribution uses only what the harness set at the run's start: the user, `TTP_PROJECT`, `TTP_TASK` and a recorded `TTP_RUN_DIR`. New `ttp leftover-procs --stop <task> [--pid N ...] [--dry-run]` does the same on request. It refuses a running task's, another task's, project's or user's processes, `ttp detach` jobs, device users, and anything it cannot attribute.
