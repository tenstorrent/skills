# Host-side environment

Every name here is an `EnvVarID` in `tt-metal/tt_metal/llrt/rtoptions.cpp`. A
misspelling is ignored silently: the run works and nothing prints.

## Selecting cores

`TT_METAL_DPRINT_CORES` is required. Defining it is what compiles printing into
the kernel, so an otherwise correct kernel prints nothing without it.

| Form | Meaning |
|---|---|
| `0,0` | One core, `x,y` |
| `(0,0),(1,1),(2,2)` | A list |
| `(0,0)-(3,3)` | An inclusive range |
| `all` | Every core of that type |
| `worker` | Worker cores |
| `dispatch` | Dispatch cores |

`worker` and `dispatch` are *values of this variable*, not separate variables.
There is no `TT_METAL_DPRINT_DISPATCH_CORES`.

Coordinates are **logical**, not virtual or physical, so worker and ethernet
cores each start at `(0,0)` and the same pair means two different cores depending
on which variable it is in. `TT_METAL_DPRINT_ETH_CORES` takes the same forms and
selects ethernet cores. `TT_METAL_DPRINT_DRAM_CORES` does the same for DRAM
cores.

## Selecting devices

Three variables, **mutually exclusive** — set one, never two. Each defaults to
`all`.

| Variable | Value |
|---|---|
| `TT_METAL_DPRINT_CHIPS` | Comma-separated chip ids, or `all` |
| `TT_METAL_DPRINT_NODES` | `FabricNodeId`s as `(Mn,Dn)`, mesh then device: `"(M0,D0),(M0,D1)"`, or `all` |
| `TT_METAL_DPRINT_MESH_COORDS` | `(row,col)` in the global system mesh: `"(0,0),(1,3)"`, or `all` |

## Selecting RISCs

`TT_METAL_DPRINT_RISCVS` takes a subset of `BR`, `NC`, `TR0`, `TR1`, `TR2`,
`TR*`, `ER0`, `ER1`, `ER*`. Default is every RISC, which on a busy core is a lot
of interleaved output; narrowing this is usually the first thing to do after a
first look.

These host-side names are not the in-kernel macro names — the macros are
`DPRINT_DATA0/1` and `DPRINT_UNPACK/_MATH/_PACK`. Resolve a mapping against
`tt-metal/tt_metal/hw/inc/api/debug/dprint.h` rather than assuming one; the
compile-time guards there are what decide which macro emits on which RISC.

## Where the text goes

| Variable | Effect |
|---|---|
| unset | The terminal |
| `TT_METAL_DPRINT_FILE=log.txt` | That file instead |
| `TT_METAL_DPRINT_ONE_FILE_PER_RISC=1` | One file per RISC under `generated/dprint/` |
| `TT_METAL_DPRINT_PREPEND_DEVICE_CORE_RISC=0` | Drop the per-line `device:(x, y):RISC:` prefix |

`_ONE_FILE_PER_RISC` **overrides `_FILE`** and forces the prefix off, since each
file already identifies its RISC. Setting both is not an error — the file named
in `_FILE` simply stays empty, which reads exactly like a kernel that printed
nothing.

`generated/dprint/` is relative to the logs directory — `get_logs_dir()` in
`dprint_server.cpp` — which is `TT_METAL_LOGS_PATH` or, unset, the working
directory the program was launched from. The doc page writes
`$TT_METAL_HOME/generated/dprint/`, which is only where it lands if you happened
to launch from the checkout.

One file per RISC is named for the device, the core and the RISC:

```
generated/dprint/device-0_worker-core-0-0_brisc.txt
```

so a missing file means that RISC was not selected or printed nothing, and an
empty one means it was selected and printed nothing — a distinction the single
combined stream cannot make.
