---
"tt-project": patch
---

`tt-project`: a wait on a remote detached job (`ttp detach --check --host <alias> --mirror <dir>`) is judged stale from the last time its mirrored log grew, and a wait on a queued or running `ttp checks` run from its own log, pid and slot-wait files, never from when the wait began. In the STATE's Expected finish, urgent tasks share a line only when they wait on the same gate for the same reason, and the line names the gate (`ETA: after landed:#N (...)`). Every estimate built for the STATE, the Course check, the web app, the started-on-host line and `ttp status` reads the project's `delivery.push_batch_max` and `delivery.push_age_s`.
