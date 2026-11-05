---
"tt-project": patch
---

`tt-project`: urgent-start alarm: a queued urgent task asleep on its own wait (a waiting hand-off not yet woken: a pending retry_when, its own checks or detached job, its retry timer) is no longer raised as 'held by nothing now'; the daemon logs it once as held by its own wait, and once woken its wait counts from the wake. The daemon's own dispatch backoffs and start_after deferrals are not an own wait: those tasks still raise the alarm, a deferred one as held by its start gate.
