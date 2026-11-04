---
"tt-project": patch
---

`tt-project`: a check sent to a fixed check host (`delivery.checks_host`, or a check's own `"host"`) no longer runs on a host that is paused. This covers this project's pause, a shared pause, a fleet hold, and another project's pause of the host's alias or `hostname`. Before, `ttp checks`, `ttp push` and the push queue reached it over ssh anyway. Now nothing connects to it: the check is the environment fault `env:check-host:<host>` with one line saying `paused by <who>: <reason>`. It waits like a check on an unreachable host, is never counted as passed or failed, and runs again once the pause lifts. A check whose host is `"local"` is unaffected.
