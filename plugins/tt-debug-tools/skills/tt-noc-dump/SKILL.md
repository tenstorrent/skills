---
name: tt-noc-dump
description: Collect NoC traces from a Tenstorrent device to find kernel data-movement mistakes — most usefully a missing noc_async_write_barrier. Use when a kernel's NoC transfers are suspect, when output is corrupt in a way that points at unflushed writes, or when asked to check barrier discipline. Not for hangs parked on a barrier (that is tt-triage).
metadata:
  tier: kernel
  upstream:
    - repo: tenstorrent/tt-metal
      ref: f9f5f3e080c4ed3d09f7fa70da81316724fa298a
      path: docs/source/tt-metalium/tools/noc_debug_dump.rst
    - repo: tenstorrent/tt-metal
      ref: f9f5f3e080c4ed3d09f7fa70da81316724fa298a
      path: tt_metal/impl/debug/noc_debugging.cpp
---

# NoC debug dump

Instruments every NoC transaction with its type, source, destination, counters
and size. The host collects those packets, buckets them per core and per RISC,
and compares each trace against earlier traces and against other cores on the
same device. At the end it prints the problems it found, grouped by core.

Experimental upstream. No kernel changes are needed — the instrumentation is
automatic.

## When to invoke

- A kernel issues NoC transfers and you suspect a missing barrier.
- Output is wrong in a way that suggests writes never landed.
- Asked to audit barrier discipline on a data-movement kernel.

Not this skill: a core parked on `noc_async_write_barrier` right now is a hang,
and the callstack comes from `tt-triage`.

## Surface

| Variable | Effect |
|---|---|
| `TT_METAL_NOC_DEBUG_DUMP=1` | Enables trace collection and the end-of-run analysis. |
| `TT_METAL_RECORD_NOC_TRANSFER_DATA` | Records transfer payload data alongside the metadata. |

## Force the state

The upstream test issues a multicast write followed by a multicast semaphore
increment with no write barrier after it — the canonical missing-barrier shape.

```bash
TT_METAL_NOC_DEBUG_DUMP=1 build/test/tt_metal/unit_tests_noc_debugging \
  --gtest_filter=NOCDebuggingFixture.McastOnlyWriteFlush
```

## Output

Printed to the console, not to a file. Emitted when the program finishes, when
the device closes, or when `ReadDeviceProfilerResults` is called explicitly.

```
========== NOC Debug Summary ==========
Unflushed async writes at kernel end
(missing noc_async_write_barrier):
    Device 0 (18,18) Processor 0 [semaphore mcast]
=======================================
```

Each finding names the device, the core coordinate, the processor index, and a
tag for the transaction kind that was left unflushed. On a multi-device run the
summary reprints and accumulates, so the same finding appears once per device
close — read the last block, and treat repeats of an earlier device's row as
history rather than as new findings.

## Traps

**It cannot run alongside watcher, the profiler, or kernel prints.** All four
compete for the same kernel binary budget, and the upstream note is explicit
that they are mutually exclusive with this feature. Unset the others first.

**A clean summary is not proof of correct barrier discipline.** The NoC is
non-deterministic: an acknowledgement can return before the trace notices the
missing barrier, so a real bug can pass. Treat a finding as evidence and a clean
run as weak.

**It costs measurable time.** Host transfers on both directions plus 1–15%
kernel cycles. Do not leave it on for a performance measurement, and do not
compare timings taken with it against timings taken without.
