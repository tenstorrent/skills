---
"tt-project": patch
---

`tt-project`: the daemon's own starts of `ttp checks --detach` (the checks it starts as a review is queued, and a restart after a reboot) pass `--no-prestage`. A project's pre-stage (`delivery.check_prestage`, or the derived one) runs in the foreground for up to 90 s by default, longer than the daemon's inline bound on that start, so on such a project the start was killed and logged as "checks not started". The full checks still run; a worker's or user's own `ttp checks --detach` keeps the pre-stage.
