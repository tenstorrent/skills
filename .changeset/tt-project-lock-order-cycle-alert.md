---
"tt-project": patch
---

The daemon's lock check builds a wait-for graph over every `ttp lock` holder and waiter on the host (own and shared locks, all projects; a party is a task or a command outside any task) and raises one critical `lock-cycle:<id>` alert at once per lock-order cycle, to the projects in it: it names every party, lock and the wait that breaks the lock order, folds the cycle's `lock-wait` alerts in, and clears once the cycle breaks. The docs state one global lock order (narrower host, device or machine locks before site- or cluster-wide ones) and that a lock wrapper outside `ttp lock` is invisible to the check. Config `locks.wide` names extra wide locks.
