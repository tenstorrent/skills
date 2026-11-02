---
"tt-project": patch
---

`tt-project`: a start_when or retry_when probe that names a variable only a worker run sets (`$TTP_RUN_DIR`, `${TTP_RESULT}`, `$TTP_TASK_ID`, ...) no longer leaves its task behind a gate that never passes. Probes run in the daemon from the project root, where those variables are unset. `ttp task set-when`, task add or update with start_when, the coordinator's task_add/task_update and a worker's waiting hand-off now write in the run's own value when the probe is set. Without a value the probe is refused, naming the variable and asking for an absolute path or one relative to the project root; a refused hand-off probe is not stored and raises a `handoff_probe_refused` event. A probe exiting 127 (a missing command or file) stays a broken probe, never "not yet".
