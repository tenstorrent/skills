---
"tt-project": patch
---

`tt-project`: A daemon that is alive but stuck (no completed tick for 10 minutes) now restarts by itself.
The systemd unit sets `WatchdogSec` and the daemon pings systemd after each completed tick; on macOS a
second launchd agent, and on cron installs the 5-minute entry, run `ttp.watchdog`, which ends a daemon
whose heartbeat stayed stale across two looks a minute apart (so waking from sleep does not trigger it),
and the service starts a new one that adopts running workers. The restart shows as an info line in the
feed. `ttp restart` and `ttp upgrade` add the watchdog to services installed earlier. The web app and
`ttp status` now say the service restarts a down or stuck daemon, and name a command only when no
service would (the project was stopped, or its service predates the watchdog).
