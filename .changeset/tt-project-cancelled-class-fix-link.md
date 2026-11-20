---
"tt-project": patch
---

`tt-project`: a correction class whose first fix task was cancelled can now be linked to the task or commit that fixed it later. `ttp task <name> superseded <cancelled id> '#<done task>'` (or a commit sha found in the harness, the project root or `--repo`) records the link, and the Course check's recurring-mistake line for that class stops while the linked task stays done. Before, `superseded` refused a cancelled task, so the line repeated every coordinator turn. An open, failed or cancelled fix task, a task of another project, an unknown task id and a missing commit are refused with a non-zero exit; `superseded` on a task that is not cancelled behaves as before.
