---
"tt-project": patch
---

`tt-project`: a daemon restart deferred because the old daemon outlived its stop timeout is now checked later. The watchdog judges the new daemon once the old one has exited, with the same start window, and rolls the harness runtime back with the same alert when it did not start; the new daemon's first good tick closes the check. A later restart or deploy supersedes it, so it never rolls back twice. launchd's `ExitTimeOut` of 0 (no timeout) now waits the 300 s cap instead of 20 s.
