---
"tt-project": patch
---

tt-project: the macOS daemon and the desktop notifier now run at launchd's normal priority (ProcessType Standard) instead of Background. Background had put the daemon and everything it starts (workers, git, test runs, `ttp push`) on the efficiency cores only, with throttled disk I/O, so ready work sat unstarted. Workers, reviewers and checks stay polite at `runner.nice`. The watchdog and kept tunnels stay Background. `ttp upgrade` rewrites an installed daemon plist to Standard with AbandonProcessGroup, keeping its other keys, and reloads it with bootout and bootstrap (kickstart keeps the old definition). When the daemon restarts itself, the reload runs in a new session. Running workers are kept: they run in their own sessions, and the daemon adopts them. An installed notifier at Background is reinstalled on upgrade.
