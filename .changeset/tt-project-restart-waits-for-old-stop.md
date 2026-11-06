---
"tt-project": patch
---

`tt-project`: a daemon restart (deploy, `ttp upgrade`, `ttp restart`) judges the new daemon only after the old one has exited. It waits for the old daemon's pid at most the service's own stop timeout (systemd's `TimeoutStopUSec`, launchd's `ExitTimeOut`, capped at 300 s) plus 15 s, and only then starts the 60 s start window. An old daemon slow to stop no longer gets a new release rolled back before it could start. A new daemon that never becomes ready still rolls back, and the alert names the failed phase; an old daemon that outlives its stop timeout defers the restart without a rollback.
