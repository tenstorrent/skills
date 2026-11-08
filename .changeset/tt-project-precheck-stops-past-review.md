---
"tt-project": patch
---

A review pre-check (the checks the daemon starts on a code task's head for its review, under state/prechecks) is stopped once no review labelled with it is still queued, running or waiting: a review already in the push queue, done or failed no longer keeps it waiting in the host checks queue. The daemon's job sweep stops it after the usual grace, unless a task's hand-off or a joined checks run still waits on it. `ttp detach --stop <prechecks folder>/checks.rc` now maps the folder to its code task and applies the usual stop rule instead of refusing it as outside a run.
