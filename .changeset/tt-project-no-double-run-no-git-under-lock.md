---
"tt-project": patch
---

`tt-project`: a running task cannot be started twice, and git never holds the database.

- The web app's Retry on a task that is running is refused with a clear error, so dispatch
  cannot start a second run of the same task. Cancel it first.
- Memory and charter commits made while a run's end is being recorded wait until that record
  is saved, so a slow git no longer blocks other writers of the project database.
