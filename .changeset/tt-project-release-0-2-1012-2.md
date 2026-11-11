---
"tt-project": patch
---

tt-project 0.2.1012: on macOS a daemon restart whose launchd agent is not loaded (the watchdog's restart, `ttp` restarts) now loads the agent from its plist and kickstarts it, so launchd starts the daemon at its plist's ProcessType (Standard). Before, the daemon was started by hand as a child of the caller and inherited the watchdog's launchd Background type (efficiency cores, throttled I/O), which no setpriority lifts. It is started by hand only when no plist is installed or launchd refuses it, and daemon.out then says it runs at its starter's priority. The daemon's and notifier's start log reports the priority `ps` reads (`cpu priority: normal (pri 31)`), with a warning when it still runs background without its plist saying so; a plist set to ProcessType Background is kept.
