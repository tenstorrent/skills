---
"tt-project": patch
---

`tt-project`: a daemon whose tick stays stalled in one step is restarted sooner, and the restart never re-enters the same hang. When the daemon's own tick watch reports a stall in a named step, its watchdog restarts it 10 to 15 minutes after its last progress (twice the stale threshold, instead of `watchdog.hang_s`), names the step, and sends a low `daemon-hang` notice when the restart worked. A run whose reap a hang or kill cut off is reaped once more with its optional checks skipped (the restriction audit, the root checkout report), with a low note; if that retry is cut off or fails too, the run is quarantined (failed, its output kept, a coordinator turn's messages left queued) instead of looping. The finish transaction commits whole or not at all, so nothing is applied twice.
