---
"tt-project": patch
---

Spill host observations (health check out and back, quarantine, spill config faults) honour observation mutes: a muted line is stored muted and counted on its mute instead of waking the coordinator. The hands-on audit counts `ttp bridge status` as a read and `ss -K` (kill sockets) as a change.
