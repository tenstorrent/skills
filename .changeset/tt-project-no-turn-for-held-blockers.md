---
"tt-project": patch
---

`tt-project`: a blocker sweep due only by a re-check ("next check due", "daily re-check due") skips model-free, with a receipt, while every item is unchanged since the last sweep (kind, status, spec, reason, hand-off, labels, dependencies, start_when probe and its exit code; the daemon's own re-sleep writes, such as the next try time, do not count) and a model sweep ran within `coordinator.blocker_sweep_recheck_skip_h` hours (default 6; 0 turns it off). A new or changed item or any other signal still gets a turn, and a re-check skipped this way stays due until a model sweep runs. The 'repeated waits' effort trigger no longer counts waits held elsewhere: a user step, a user pause or a resource the user paused, a peer that is recovering or listed in the project's `peer_recovery.json`, or a machine under a known outage that only other projects recorded. Every other repeated wait still raises it.
