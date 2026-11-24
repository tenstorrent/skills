---
"tt-project": patch
---

`tt-project`: after a host reboot, the runs it cut short restart on the daemon's first pass, right after the reap, instead of waiting minutes for the first tick's slow steps (git reconciles, post-landing checks, the worktree prune) and the post-outage ramp. The resume pass keeps every dispatch gate (pause, settle, host cycle, dollar caps, locks, resources); only the ramp exempts it, since it restores work that ran. A run recorded on an earlier boot no longer counts as alive because of a young lease: only its own supervisor still up, with the recorded pid and start time, keeps it. A run alive on this boot is never started twice.
