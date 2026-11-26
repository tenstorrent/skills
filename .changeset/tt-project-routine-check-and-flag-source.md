---
"tt-project": patch
---

The routine-coordinator check no longer marks a wake non-routine for a pending session restore (a lane turn never
touches the session) or for a course line that only mentions a promise; due or missed promises still count. A
wake-cost shrink request lasts one wake, so a session too small to compact no longer keeps it set. The digest's
Rollout flags line and `ttp rollout status` show each flag's effective state and its source, worded alike.
