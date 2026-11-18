---
"tt-project": patch
---

Stay-off watchdog: only a device job on a held box counts (a bare remote job there needs declared device use); aliases of one box give one alert per job; an open stream's alert says live since its start, not "quiet until"; new `occupancy.alerts` switch (on by default, coordinator-settable) pauses posts and events while flags stay in status; the after-push deploy summary lists each held project with the version it runs and how many releases it is behind (user msg #3518).
