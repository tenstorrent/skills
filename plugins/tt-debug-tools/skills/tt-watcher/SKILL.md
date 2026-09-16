---
name: tt-watcher
description: Watch a Tenstorrent device from a polling thread inside the run — catch bad NoC transactions, circular-buffer overflows, L1 overflows, tripped kernel asserts, stack overflows, and where each RISC is parked. Use when a kernel corrupts memory, a transfer targets a core that does not exist, a run hangs and you can relaunch it, or a watcher.log needs interpreting. Turn it on before launching; tt-triage is what you reach for when you cannot.
metadata:
  tier: kernel
  upstream:
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: docs/source/tt-metalium/tools/watcher.rst
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: tt_metal/impl/debug/watcher_server.cpp
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: tt_metal/impl/debug/watcher_device_reader.cpp
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: tt_metal/tools/watcher_dump
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: tt_metal/llrt/rtoptions.cpp
---

# tt-watcher

A host thread that polls every core on every device on an interval, reads the
watcher mailboxes out of L1, and writes one dump per interval to `watcher.log`.
It checks each NoC transaction against the core map before it is issued, so it
names the faulting core and the offending address rather than leaving a hang. The
checks compile into the kernel, so this is decided before you launch — nothing
here attaches to a run in progress, which is what `tt-triage` is for.

## When to invoke

- A kernel writes somewhere it should not, or a transfer targets a core that does
  not exist or straddles the end of L1.
- Output is corrupt in a way that points at a bad address rather than bad math.
- A run hangs and you can relaunch it. Watcher shows where each RISC parked.
- You have a `watcher.log` or a console fault report and need it read.
- An assert fired: it halts the core, and watcher is one of the two things that
  report it (`tt-asserts`).

Not this skill: already hung, no relaunch — `tt-triage`.

## Surface

| Variable | Effect |
|---|---|
| `TT_METAL_WATCHER=<N>` | Enable, polling every `N` **seconds**. A bare number is seconds; suffix `ms` for milliseconds (`250ms`). |
| `TT_METAL_WATCHER_APPEND=1` | Append to the existing log instead of truncating it. |
| `TT_METAL_WATCHER_DUMP_ALL=1` | Also dump state that is unsafe to read while a kernel runs. |
| `TT_METAL_WATCHER_NOINLINE=1` | Stop inlining the checks, to shrink the kernel. |
| `TT_METAL_WATCHER_DISABLE_<FEATURE>=1` | Turn one check off. Eleven of them; full list in `references/features.md`. |
| `TT_METAL_WATCHER_DEBUG_DELAY=<cycles>` | Stall NoC operations to force a race. Needs a target: `TT_METAL_{READ,WRITE,ATOMIC}_DEBUG_DELAY_CORES` and `_RISCVS`. |

In-kernel, compiled in only while watcher is enabled:
`WATCHER_RING_BUFFER_PUSH(uint32_t)` pushes to a 31-element per-RISC ring buffer,
and `PAUSE()` halts the kernel until released — `references/features.md`. Delay
targets and their two preconditions: `references/delays.md`.

## Force the state

Upstream's sanitize suite covers seventeen fault modes and asserts on the exact
string each produces:

```bash
TT_METAL_LOGS_PATH=$PWD build/test/tt_metal/unit_tests_debug_tools \
  --gtest_filter=MeshWatcherFixture.TensixTestWatcherSanitize
```

This case reports a BRISC unicast write to a core that does not exist, then exits
about 1.5s later — not a hang, and not a triage target.

## Output

Two destinations, and confusing them wastes a run.

**A fault report goes to the process's stdout**, not the log: the offending
transaction, `Last waypoint:`, the ring buffer, and `While running kernels:` with
a kernel name per RISC. Grep the run's own output for `Watcher detected NOC
error and stopped device`.

**The per-interval dumps go to `watcher.log`**, one block headed `Dump #N at
<t>s`, closed by `Dump #N completed`, one line per core with its waypoints, run
messages and kernel ids.

Both shapes field by field: `references/log-format.md`. Waypoint vocabulary:
`references/waypoints.md`. Reading these structures out of a dead or unwatched
process: `references/dump-without-watcher.md`.

The log lands at `<logs_dir>/generated/watcher/watcher.log`, where `logs_dir` is
`TT_METAL_LOGS_PATH` or, unset, **the working directory** — not `TT_METAL_HOME`.
Set it, or the log follows wherever you launched from.

## After a trip: hand off to triage

A trip halts the device with the Inspector RPC still serving the hung process
— the state `tt-triage` reads best. Do not kill it to look at `watcher.log`:
the per-core callstacks and integrity checks need the live process. See
`tt-triage`.

## Traps

**Watcher, DPRINT and the device profiler share on-chip SRAM.** Enabling more
than one silently corrupts the debug data — no error, just wrong output. Unset
the other two.

**A trip is not proof of a regression.** These checks were never running before,
so an overflow that appears under `TT_METAL_WATCHER` was latent all along — a
prior green run without watcher is not evidence the code was ever correct.

**Longer intervals are less invasive, not weaker.** Polling perturbs timing, so
a long interval suits a hang that comes and goes and a short one a deterministic
fault; reaching for a shorter interval on an intermittent bug is backwards. Same
reason `_DUMP_ALL` needs a long one — it reads state that is unsafe to touch
mid-kernel and will hang the kernel it is inspecting otherwise.

**Disabling a feature removes it from the kernel, not just from the report.**
`TT_METAL_WATCHER_DISABLE_PAUSE` does not leave a kernel parked at `PAUSE()` — the
macro compiles to nothing, and upstream tests exercising a disabled feature skip
rather than fail.

**The variable is `TT_METAL_WATCHER_DISABLE_SANITIZE_NOC`.** Upstream's own
assertion messages name `TT_METAL_WATCHER_DISABLE_NOC_SANITIZE`, which does not
exist — setting it does nothing and the assertion still fires.

**Over the fabric-kernel binary limit**, shed in order: `_DISABLE_SANITIZE_NOC`, `_NOINLINE`, `_DISABLE_ASSERT`.
