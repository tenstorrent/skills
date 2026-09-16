# Feature flags and in-kernel calls

Every name below is an `EnvVarID` in `tt-metal/tt_metal/llrt/rtoptions.cpp`. A
name that is not in that list is silently ignored — the run looks fine and the
feature never turns on.

## Enable and behaviour

| Variable | Effect |
|---|---|
| `TT_METAL_WATCHER=<N>` | Enable, poll every `N` seconds. `<N>ms` for milliseconds. |
| `TT_METAL_WATCHER_APPEND=1` | Append rather than truncate the log at startup. |
| `TT_METAL_WATCHER_DUMP_ALL=1` | Include state unsafe to read mid-kernel. Needs a long interval. |
| `TT_METAL_WATCHER_NOINLINE=1` | Do not inline the checks. Shrinks the kernel. |
| `TT_METAL_WATCHER_TEXT_START=1` | Report addresses relative to the start of `.text`. |
| `TT_METAL_WATCHER_PHYS_COORDS=1` | Report physical rather than virtual core coordinates. |
| `TT_METAL_WATCHER_SKIP_LOGGING=1` | Run the checks, send the log to `/dev/null`. |
| `TT_METAL_WATCHER_TEST_MODE=1` | Record the fault message for a test to read instead of tearing the run down. |
| `TT_METAL_WATCHER_DEBUG_DELAY=<cycles>` | Inject stalls — `references/delays.md`. |
| `TT_METAL_WATCHER_ENABLE_NOC_SANITIZE_LINKED_TRANSACTION=1` | Also sanitize linked transactions. Requires NoC sanitization on. |

## The eleven disable flags

Each is `TT_METAL_WATCHER_DISABLE_<name>=1`.

| Name | Turns off |
|---|---|
| `ASSERT` | Reporting of tripped kernel asserts |
| `PAUSE` | Honouring `PAUSE()` |
| `RING_BUFFER` | The in-kernel ring buffer |
| `STACK_USAGE` | Stack high-water tracking and overflow warnings |
| `SANITIZE_NOC` | NoC transaction checking — the main check |
| `SANITIZE_READ_ONLY_L1` | Read-only L1 region checking |
| `SANITIZE_WRITE_ONLY_L1` | Write-only L1 region checking |
| `WAYPOINT` | Waypoint recording |
| `DISPATCH` | Watching dispatch cores |
| `ETH` | Watching ethernet cores |
| `CB_SANITIZE` | Circular-buffer bounds checking |

**`SANITIZE_NOC`, not `NOC_SANITIZE`.** Three assertion messages in
`rtoptions.cpp` name `TT_METAL_WATCHER_DISABLE_NOC_SANITIZE`, which is not a real
variable. Setting it changes nothing and the assertion those messages belong to
still fires.

**Disabling compiles the feature out.** The flags do not merely quiet the report:
the kernel-side macro becomes a no-op. `DISABLE_PAUSE` does not leave a kernel
parked at `PAUSE()`, and `DISABLE_ASSERT` does not leave an assert to be found
later. Upstream tests that exercise a disabled feature `GTEST_SKIP` rather than
fail, so a green run proves nothing about a feature you turned off.

## In-kernel calls

Both are macros that expand to nothing unless watcher is enabled, so leaving them
in place costs nothing in a normal build.

`WATCHER_RING_BUFFER_PUSH(uint32_t)` —
`tt-metal/tt_metal/hw/inc/api/debug/ring_buffer.h`. A 31-element ring buffer per
RISC. The dump prints it newest-first on a fault, and `tt-triage`'s
`dump_watcher_ringbuffer` reads it separately.

**There is no cross-RISC synchronisation.** Two RISCs on one core pushing to the
buffer is undefined behaviour, not interleaved output. Push from one RISC per
core.

`PAUSE()` — `tt-metal/tt_metal/hw/inc/api/debug/pause.h`. Halts the kernel until
released from the host. Paused cores are listed in the dump.
