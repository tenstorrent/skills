---
"tt-project": patch
---

`tt-project`: process-liveness checks run from a worker sandboxed in its own PID namespace no longer read every host process as dead. A shared helper (`liveness.pid_state`) returns alive, dead or unknown, and returns unknown when the caller's PID namespace differs from the daemon's (`TTP_PIDNS`) or from the one a record names. Its callers treat unknown as possibly alive and say "unknown (other PID namespace)": `ttp detach --check` of a series driver, spill-host check claims in `ttp status`, `ttp listen` replacement (an older listener it cannot place is left alone, never signalled or waited on) and chat listener checks, progress stall reports, route leases, the upgrade journal, `ttp checks` (a run never prunes, joins or counts as lost a check run of another namespace; it queues on its own), the device-queue probe (which says "unknown" instead of "gone"), and `ttp setup` recovery, which no longer rolls back a release under a setup it cannot see.
