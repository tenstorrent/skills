---
"tt-project": patch
---

`tt-project`: when push batches are held `script_torn` on the same check `delivery.push_script_torn_escalate_after` times in a row (default 3, 0 = off), the push queue raises one high `push_script_torn` alert to the coordinator naming the check, the scripts and the batches. Each check counts on its own; the alert clears once that check passes in a batch, or a batch pushes.
