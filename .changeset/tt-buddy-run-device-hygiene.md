---
"tt-buddy": patch
---

`run`: device-hygiene guidance for shared devices.

- Job timeout set to the run's envelope (~2–3× known runtime), not the
  backend maximum; every wait or poll bounded by it.
- Long-run watchdog: tell a slow compile from a wedge, triage before killing.
- Fatal-wedge log signatures that mean "start recovery now".
- Any shell kill of a device job is forbidden, not just `kill -9`.
- Keep host work and kernel JIT out of the device reservation.
- tt-device-mcp limits corrected: exec default 180s (max 600s), job timeout
  default 600s (hard max 1500s), `output_tail` and log-size caps.
- No manual reset after a kill when the broker already resets and runs its
  full health check; verify through `tt_device_queue_status`, not `tt-smi -s`.
