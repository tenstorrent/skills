---
"tt-project": patch
---

`tt-project`: a deferred daemon restart's later check now ends with no verdict only when the harness runtime itself changed, so a memory or charter commit no longer drops it. A rollback the watchdog makes judges the restarted daemon only until shortly before the watchdog's own 300 s limit, so its alert is always posted (saying the restart was not yet confirmed when time ran out); with too little time left it leaves the check to the next watchdog run.
