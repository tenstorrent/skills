---
"tt-project": patch
---

`tt-project`: a `ttp checks` run whose owning run has ended no longer waits in, or holds a slot of, the host-wide checks queue. A model-free daemon pass (leftoverchecks, about once a minute) stops this user's leftover checks runs (working folder deleted, run folder gone, run ended, or task finished) with SIGTERM, then SIGKILL, through the audited stop path. It drops their queue tickets and logs each stop to daemon.log and the checks waits log. Live runs, other users' processes, other projects' runs, daemon review prechecks and `ttp lock` holders are never touched. `ttp checks --reap [--dry-run]` runs the same pass by hand.
