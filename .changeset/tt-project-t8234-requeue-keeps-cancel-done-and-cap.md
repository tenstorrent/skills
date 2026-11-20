---
"tt-project": patch
---

`tt-project`: a plain `ttp task requeue` (the path model-free scripts use) no longer undoes a cancel or a done hand-off or runs a task that failed its last allowed attempt; `--force` and the web app's Requeue are the deliberate route, on record. Dispatch also refuses a task whose last run failed at its attempt limit and was queued again with no explicit requeue, and puts it back to failed.
