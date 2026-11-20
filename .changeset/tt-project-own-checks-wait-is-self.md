---
"tt-project": patch
---

`tt-project`: a waiting hand-off on the project's own checks lane (its `ttp checks` result, a run's `checks.rc`, or a `waiting_for` naming the checks slot, lane, queue or run) is now recorded as a self-wait (why `checks`), even when the worker wrote `wait_kind: external`. External wait time, the stuck and selfblock views and the daily audit no longer count the project's own checks as someone else's. A probe that also waits on other work (a lock, another task or project, an upstream fix, the network or another host) keeps the hand-off's word; `wait_kind: self` and `user` are unchanged.
