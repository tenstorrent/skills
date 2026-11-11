---
"tt-project": patch
---

`tt-project`:

- on macOS a restart or install reloads a launchd agent by `launchctl bootout`, then waits (at most 30 s, polling `launchctl print`) until launchd has removed the job, then bootstraps it, retried up to 3 times; a job launchd already loaded counts as loaded. A bootstrap failure keeps launchctl's own words. A reload the daemon runs on itself does the same from a new session and logs a failure to `logs/daemon.out`.
- a restart whose bootstrap launchd refuses fails with that reason (logged to `logs/daemon.out`, shown by `ttp restart`) and never starts the daemon by hand: a hand start ran outside launchd at its starter's CPU priority (nice 19) with no KeepAlive. With no launchd agent installed the hand start stays.
- the launchd watchdog bootstraps an unloaded daemon agent (after ending a hung daemon, and when no daemon runs), since KeepAlive restarts a daemon only while its agent is loaded. It runs as ProcessType Standard (an older Background watchdog plist is rewritten and reloaded on upgrade) and never rewrites or boots out its own job from inside it (`XPC_SERVICE_NAME`); tests with a faked launchctl.
