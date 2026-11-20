---
"tt-project": patch
---

`tt-project`: the worker's Claude Code Stop hook runs the project's hand-off checks (`worker.handoff_checks`) on a done, needs-review, blocked or failed hand-off, and a check that exits 1 refuses the stop with its output so the same session fixes it instead of a new run. Each refusal counts toward `worker.handoff_check_max_sendbacks`; a broken or slow check never blocks the stop, and the daemon's run of the checks stays the final gate. `ttp leftover-procs` run from the Stop hook skips the session's own child processes.
