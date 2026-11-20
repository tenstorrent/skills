---
"tt-project": patch
---

`tt-project`: a task can withdraw its own detached push with `ttp push --stop <marker>`, or with `ttp detach --stop <marker>` on a push marker, so the push train can take over its head. A queued push is recorded as not started. A push that is waiting for the push lock or running its checks gets SIGTERM to its own process, so its checks end with it. A SIGKILL to its group follows only if it has not begun its `git push`. The marker, log and run lock file are then removed. A push that has begun its `git push` is never interrupted: the stop says so and exits 1, and the push ignores stop signals from that point on. The stop refuses another task's push, another project's push, and the push queue's batch. The kill guard's advice names this route.
