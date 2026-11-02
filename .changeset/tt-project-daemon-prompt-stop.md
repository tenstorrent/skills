---
"tt-project": patch
---

`tt-project`: a stop request (SIGTERM or SIGINT) ends the project daemon within seconds, even during a slow tick, so systemd no longer force-kills it after its stop timeout. The running tick step finishes what it began and the tick's remaining steps are skipped (one `stop: skipped steps ...` log line); the wait between ticks ends at once; `daemon stop requested` names the step that was running. The clean stop record, the stop of schedule runs and of the end, pause, mute and machine probes now run on every stop, so the next start no longer counts a crash. The systemd unit sets `TimeoutStopSec=120` explicitly and keeps `KillMode=process`, so workers still outlive a daemon restart.
