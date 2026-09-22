---
name: tt-dprint
description: Print values out of a Tenstorrent kernel — scalars, bfloat16, enums, and circular-buffer tile contents — with DPRINT / DEVICE_PRINT. Use when a kernel produces wrong numbers and you need to see what it actually read or wrote, when you want a checkpoint reached or a loop index, or when a DPRINT log needs interpreting. For a kernel that hangs rather than computes the wrong answer, that is tt-watcher or tt-triage.
metadata:
  tier: kernel
  upstream:
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: docs/source/tt-metalium/tools/device_print.rst
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: tt_metal/hw/inc/api/debug/dprint.h
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: tt_metal/hw/inc/api/debug/device_print.h
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: tt_metal/llrt/rtoptions.cpp
---

# tt-dprint

Kernels call `DPRINT`, a host-side server polls the per-RISC buffers and writes
what it finds to the terminal or a file. The device side is API calls compiled
into the kernel; the host side is environment variables deciding which cores are
read and where the text goes.

`DPRINT` is a thin alias for `DEVICE_PRINT` and takes an `fmt`-style format
string. **The stream form `DPRINT << x << ENDL()` has been removed** — the
operators and `BF16`/`F32`/`U32`/`HEX` helpers are deprecated stubs, so anything
written in that style is stale.

## When to invoke

- A kernel computes the wrong values and you need to see its inputs, its
  intermediates, or a CB's tile contents.
- You want to know whether a branch was taken or a loop ran to its bound.
- You have a DPRINT log and need it read, including tile dumps.

Not this skill: nothing prints and the run *hangs* — `tt-watcher`.

## Surface

`TT_METAL_DPRINT_CORES` is load-bearing twice over: it selects the cores the host
reads, and **whether it is defined at all decides whether printing is compiled
into the kernel.** Coordinates are logical — worker and ethernet both start at
`(0,0)`.

```bash
TT_METAL_DPRINT_CORES=0,0 ./your_program   # also (x,y),(x,y) | (x,y)-(x,y) | all | worker | dispatch
```

| Variable | Effect |
|---|---|
| `TT_METAL_DPRINT_CORES` | Required. Worker cores to read, in the forms above. |
| `TT_METAL_DPRINT_ETH_CORES` | Same forms, for ethernet cores. |
| `TT_METAL_DPRINT_RISCVS` | Subset of `BR,NC,TR0,TR1,TR2,TR*,ER0,ER1,ER*`. Default all. |
| `TT_METAL_DPRINT_FILE` | Write to a file instead of the screen. |
| `TT_METAL_DPRINT_ONE_FILE_PER_RISC` | One file per RISC under `generated/dprint/`. |
| `TT_METAL_DPRINT_PREPEND_DEVICE_CORE_RISC` | On by default; prefixes each line with device, core and RISC. |

Device selection is a mutually exclusive trio — `TT_METAL_DPRINT_CHIPS`, `_NODES`, `_MESH_COORDS`. Full forms and defaults: `references/env.md`.

In-kernel, after `#include "api/debug/dprint.h"`: `DPRINT` plus the per-RISC
variants `DPRINT_MATH`, `DPRINT_PACK`, `DPRINT_UNPACK`, `DPRINT_DATA0`,
`DPRINT_DATA1`. Types, format specs, `CTSTR` and enums:
`references/types-and-format.md`. Tiles out of a circular buffer:
`references/tile-printing.md`.

## Force the state

Any program whose kernels print, with the cores named:

```bash
TT_METAL_DPRINT_CORES=all build/programming_examples/metal_example_noc_tile_transfer
```

Upstream's `device_print/` gtest suite covers every scalar type, the format
specifiers, tile printing and concurrent RISCs — but its fixture routes DPRINT to
a memfd to assert on it, so running that binary shows gtest output and no prints.
Read those cases for the API; run an example to see the tool work.

## Output

To the terminal by default, one line per print, prefixed unless you turn the
prefix off:

```
0:1-7:BR: dispatch_11: start
0:7-7:BR: REALTIME BRISC: kernel started
```

`<device>:<x>-<y>:<RISC>:` then the formatted text — the doc page writes the
coordinate as `(x, y)`, but hardware emits `x-y`. With
`_ONE_FILE_PER_RISC` the prefix is disabled automatically and the files land
under `generated/dprint/`, not at `TT_METAL_DPRINT_FILE`.

## Traps

**Every `DPRINT` must end with `\n`.** The host server splits each per-RISC
stream on newlines and holds anything else in an intermediate buffer. A trailing
partial line is **not** flushed when the device closes — it is lost. When prints
do not appear at all, check this before anything else; it is the most common
cause by upstream's own account.

**`TT_METAL_DEVICE_PRINT=1` does not exist.** Prior-art agent docs prescribe it.
The only `TT_METAL_DEVICE_PRINT_*` variables are the three dispatch-timing ones,
which have nothing to do with printing. Enabling is `TT_METAL_DPRINT_CORES`.

**It cannot share a run with watcher or the device profiler.** All three use the
same on-chip SRAM and enabling more than one silently corrupts the debug data.
Unset the others.

**A runtime `const char*` prints as an address**, because the host cannot read
device memory — wrap literals in `CTSTR()`. Relatedly, the MATH RISC cannot reach
circular buffers, so `DPRINT_MATH` with a `TSLICE` is invalid rather than empty.

**Tile prints sample the pointer, not the tile.** A CB read has to sit between
`cb_wait_front` and `cb_pop_front`, a write between `cb_reserve_back` and
`cb_push_back`. Outside that window the pointer has moved and the values belong
to another tile.

**`_ONE_FILE_PER_RISC` overrides `TT_METAL_DPRINT_FILE`.** Setting both is not an
error and the file you named stays empty.

**`TT_METAL_DPRINT_CORES=all` includes the dispatch cores**, whose firmware
prints heavily on its own — `dispatch_11: start`, `CQ_DISPATCH_SET_*`,
`prefetcher_11`. Your kernel's lines arrive interleaved with all of it. Name the
cores you care about instead, or narrow with `TT_METAL_DPRINT_RISCVS`.
