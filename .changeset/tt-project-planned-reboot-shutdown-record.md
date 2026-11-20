---
"tt-project": patch
---

`tt-project`: a host reboot now counts as orderly when, after the daemon's last heartbeat, wtmp holds a shutdown record or the daemon got a stop signal (SIGTERM/SIGINT, as when the service manager stops its units at a shutdown), not only when the daemon wrote its clean-stop record. A daemon stop that outlasted the unit's stop timeout was killed before that record, so an ordered reboot was called abrupt and the runs it cut short that held a device were blocked; they are now requeued with their run folders kept. The stop-signal record is written to the state folder the moment the signal arrives and is cleared at each daemon start. Reading wtmp is best effort: a missing, unreadable or foreign-format file keeps the abrupt path. The boot record names which sign made it orderly (`orderly_by`).
