---
"tt-project": patch
---

`tt-project`: a process a device-queue job leaves running (a `setsid ... &` server, a background child) no longer keeps the job's lock. The `devq` runner's job lock is now held by a small keeper that lives exactly as long as the job's wrapper, so a successor runner reclaims an interrupted job instead of waiting on a leftover. A detached push's run lock is made close-on-exec in the push process, so nothing it starts can hold it. A `ttp detach` job lock is still inherited by every process of the job on purpose (it is how `jobstop` finds escaped processes; readers take the `.rc` first); its label now names the job's wrapper with `via: job`, and the stale-lock reaper leaves such locks alone. A detached push's and a push batch's run lock now name the push process as their holder once it starts, so the stale-lock reaper never stops a live push after the `ttp push --detach` (or the daemon) that took the lock has exited.
