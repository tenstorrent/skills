---
"tt-project": patch
---

`tt-project`: a finished task's own daemon review precheck (run folder `prechecks/t<task>-<head>`, no numeric run id) that still holds a checks lane past `checks.done_holder_grace_s`, with its review closed, is now stopped and the lane freed (`checklane.stop_finished` resolves the folder and checks its `checks.pid`).
