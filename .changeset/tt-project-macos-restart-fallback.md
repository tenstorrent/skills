---
"tt-project": patch
---

`tt-project`: on macOS, restarting a daemon that has no loaded launchd agent (started by hand, or
its bootstrap failed) stops the old daemon and starts a new one, as on Linux without systemd,
instead of failing and rolling the runtime back.
