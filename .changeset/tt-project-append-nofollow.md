---
"tt-project": patch
---

`tt-project`: the daemon's appends to state and run-folder files (durable_append, so host-run.jsonl and other logs carrying worker text, plus daemon.log, the churn and scheduler logs, the stack dump file and the runner's progress notes) never follow a symlink or open a FIFO; a refused append is logged and kept as a handled event with its text.
