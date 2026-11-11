---
"tt-project": patch
---

Detached jobs: a wait on a live detached job raises wait_stale only once the job's log, start and queue marks stop advancing for waiting.max_hold_s, never by the time since the wait began. A job that died by a signal no tt-project stop recorded gets a job-signals.jsonl row when its record closes (reason "unseen", the signal from its exit code). detached.json keeps the job's full command line and its argv.
