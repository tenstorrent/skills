# Script inventory

Auto-discovered from `tt-metal/tools/triage/`. Two kinds: **data providers**
return data others depend on, **state checkers** are named `check_*`. A provider
failure skips its dependents.

## What was running

| Script | Tells you |
|---|---|
| `dump_running_operations` | Ops in flight, with shapes and dtypes. Your anchor to the workload. **Lowest op id hung first.** `(trace id: N)` means trace-replayed. |
| `dump_op_mesh` | Mesh-wide dispatched ops. `(idle)` opened but no live op, `(unused)` never opened, `(remote)` on another host. A leading `[!]` marks a **straggler** lagging the dispatch leading edge — start there. |
| `dump_op_window` | Ops around the running set. `RUNNING` means currently in dispatcher mailboxes. Shows what ran just before. |

## Why it is stuck

| Script | Tells you |
|---|---|
| `dump_callstacks` | Where each RISC is parked. Usually the most useful output in the report. |
| `dump_aggregated_callstacks` | The same, grouped by stuck point, with a count of cores and devices waiting at each. Reach for it on many-core or multi-device hangs. |
| `dump_lightweight_asserts` | Asserts that fired: callstack, template params, runtime args, locals. Distinguishes a while-loop spin, a trap, and an intentional assert. |
| `dump_watcher_ringbuffer` | The in-kernel ring buffer, if a kernel pushed to it. |
| `dump_fast_dispatch` | Dispatch-core state along the fast-dispatch path. Secondary; its own errors are rarely the cause. |
| `dump_mesh_sockets` | Mesh socket state. |
| `check_cb_inactive` | Circular buffers with no activity. |

## Integrity

| Script | Tells you |
|---|---|
| `check_binary_integrity` | On-device `.text` against the ELF. `Data mismatch in section .text` means something overwrote code — a wild pointer or a core writing memory it does not own. `Restricted / Unsafe access` means triage read at a wrong kernel offset, so mailboxes may be corrupt. |
| `check_core_magic` | Each core's magic number against the expected firmware type. A corruption report here invalidates every mailbox-reading script above. |

## Hardware and fabric

| Script | Tells you |
|---|---|
| `check_noc_status` | NoC transaction state. "Mismatched state" is common across hangs and is a diagnostic observation, not a root cause on its own. |
| `check_eth_status` | Ethernet links and fabric. Note down or degraded links on multi-device runs. |
| `check_noc_locations` | NoC coordinate and block-location sanity. A mismatch points at tt-umd or topology setup, not a kernel bug. |
| `check_l1_status` | L1 state. |
| `check_arc` | ARC reachable. **Compare uptime across ARCs:** one much smaller means it died and reset mid-run; very high and predating the workload means the device was already odd before you started. |
| `arc_heartbeat_sampling` | ARC heartbeat over time. |
| `device_telemetry` | Power, thermal and clocks at triage time. Catches a pinned low clock or high temperature. Will not catch a sub-millisecond current droop. |
| `device_info`, `firmware_versions`, `system_info` | Board identity, firmware versions, host environment. Firmware mismatches show up here. |
| `check_broken_components` | Cores triage could not inspect cleanly. Mostly its own artifact — see the trap in `SKILL.md`. |
| `dump_risc_debug_signals` | RISC debug signals for broken cores. Often absent with no broken cores, which is normal. |

## Support

`dump_configuration`, `parse_inspector_logs`, `dispatcher_data`,
`inspector_data`, `callstack_provider`, `elfs_cache`,
`metal_device_id_mapping`, `operation_provider`, `operation_param_parser`,
`configuration_provider` — providers and helpers rather than findings.
