---
"tt-project": patch
---

`tt-project`: urgent-start alarm: a queued urgent task asleep on its own wait (a pending retry_when, its own checks or detached job, a retry timer) is no longer raised as 'held by nothing now'; the daemon logs it once as held by its own wait, and its wait time counts from when that wait passed.
