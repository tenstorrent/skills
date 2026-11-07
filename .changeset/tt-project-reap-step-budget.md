---
"tt-project": patch
---

Daemon: ending finished runs no longer holds a tick for minutes. The run-ending step now keeps the shared step time budget: once spent, it stops and the runs left (still on record with their exit file on disk) are ended on the next tick or by the next daemon, each exactly once. The git reads that warm up one run's end (such as a failed review's area check, which read git once per recent failed review) share a 60 s budget, and the reads a run's end learns between its write rounds now end by the rounds' wall, cut from 240 s to 90 s. What is not read in time degrades as a git failure does; no run record, hand-off, message or follow-up is dropped.
