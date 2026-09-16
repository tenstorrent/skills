# Output shapes

Two artifacts, produced by different code paths. The console report is the one
that names a fault; the log is the one that shows every core.

## The console fault report

Written to the process's stdout/stderr the moment a check trips, not to the log:

```
Watcher detected NOC error and stopped device:
Device 0 worker core(x= 0,y= 0) virtual(x=18,y=18): BRISC using noc0 tried to
unicast write 102400 bytes from local L1[0x155000] to Unknown core w/ virtual
coords 26-18 [addr=0x00123000] (NOC target address did not map to any known
Tensix/Ethernet/DRAM/PCIE core).
Last waypoint: NAWW,   W,   W,   W,   W
While running kernels:
 BRISC: tests/.../dataflow/dram_copy_to_noc_coord_2_0.cpp
 NCRISC: blank
 TRISC0: blank
```

Read it in this order: which RISC on which core, what it attempted, the
parenthesised reason, then the waypoint for where it was, then the kernel name
for what was running. `blank` means no kernel on that RISC, not a missing name.

The address is the *source* in local L1; the target is the coordinate pair and
`addr=`. `Unknown core` means the coordinate does not exist on this part —
usually arithmetic on a core index, not a bad address.

## `watcher.log`

Header, then one block per poll:

```
At 0.361s starting
Legend:
        ...
At 0.361s attach device 0
-----
Dump #1 at 0.362s
Device 0 worker core(x= 0,y= 0) virtual(x=18,y=18):   GW,   W,   W,   W,   W  rmsg:D0D|bnt h_id:  0 smsg:DDDD
k_ids:  0|  0|  0|  0|  0
...
k_id[  0]: blank
Dump #1 completed at 0.410s
```

Per-core line, left to right:

| Field | Meaning |
|---|---|
| `worker` / `acteth` / `idleth` | Core kind. Also dram cores on parts that have them. |
| `core(x,y)` | Logical coordinate. `virtual(x,y)` is the NoC coordinate the fault text uses. |
| five codes | Waypoint per RISC, in the legend's order: BRISC, NCRISC, TRISC0, TRISC1, TRISC2. `references/waypoints.md`. |
| `rmsg:` | BRISC run message. `D`/`H` device or host dispatch, NOC id, `I`/`G`/`D` init/go/done, then enable flags where UPPER is enabled: `B`/`b` BRISC, `N`/`n` NCRISC, `T`/`t` TRISC. |
| `smsg:` | Subordinate run messages, `I`/`G`/`D` each. |
| `h_id:` | Host id of the dispatched program. |
| `k_ids:` | Kernel id per RISC, resolved by the `k_id[N]:` map at the end of the device's section. |

The legend at the top is generated from the part's own processor list, so trust
it over any fixed ordering.

`Dump #N completed` is written after the block and flushed. A block without it is
the poll that was still in progress — or the one that faulted, because the fault
path leaves the file before flushing.

## Two ways the log misleads

**A completed block is not evidence a kernel ran.** The first dump usually lands
before the first launch: every core reads `GW` with `k_ids: 0` and the map says
`k_id[ 0]: blank`. That is an idle device, not a stuck one.

**After the run, the log is not trustworthy.** Unless
`TT_METAL_WATCHER_APPEND=1`, startup truncates it, and a fault path that
truncates while a buffered block is outstanding leaves a hole padded with blanks
and a fragment of the last dump. Read the log while the process is alive, or set
append.
