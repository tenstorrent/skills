---
"tt-project": patch
---

tt-project: checks that run on another host are gated on that host's load, not this one's. A push or `ttp checks` run whose checks all run elsewhere (a check's own `"host"`, `delivery.checks_host` or a spill host), with at most light checks here, takes no host-wide checks slot and no test slots on this host and never waits for its CPU load guard. It waits instead while the remote host's runnable demand (one ssh read of its `/proc/stat`, cached 30 s) is above `checks.load_guard` times its cores, at most `checks.push_load_wait_s`, and says which host holds it; an unreadable host never holds it. Local checks keep the load guard, checks slot and test slots as before. `claude plugin validate` is now a light check.
