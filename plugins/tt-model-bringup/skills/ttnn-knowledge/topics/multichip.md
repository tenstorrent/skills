# Multi-chip and CCL

**Blackhole's worker grid is 11 columns wide; many configs assume 8.** This bit three ways:
- SDPA decode derives reducer coordinates from the compute grid width while query shards sat on the first 8
  columns; an explicit 8-wide SDPA program config then read the wrong users once the batch exceeded 8. Matching
  the SDPA grid to where the shards live, or keeping q interleaved in DRAM for batched SDPA, fixed it.
- A CCL manager initialized global semaphores on an 8×8 grid while the default reduce-scatter and all-gather
  workers ran on the 11×10 grid outside it; the first (cold) call returned wrong results. Allocating semaphores
  over `compute_with_storage_grid_size()` fixed it.
- All-gather worker coordinates sorted by x wrapped the 11-column grid with two links and two workers per link.
SDPA also expects rectangular, row-major core ownership; a core-range set built from single cores sorts by x then
y, so build it in the intended order.

**Fused CCL ops.** All-gather fused with matmul and matmul fused with reduce-scatter hung, returned NaN or wrong
values on 4-chip Blackhole meshes in several bringups: the fused reduce-scatter factory built ring kernels for a
linear topology; worker and receiver indexing assumed one N or M block per core; readers ignored some fusion
arguments with DRAM-sharded weights. Repaired variants usually measured slower than the separate ops. Measuring
the unfused path first and treating fused CCL as an experiment saved time. Fused all-gather+matmul ops also
require the gathered dimension to be the last one.

**Uploading to an open mesh.** `ttnn.from_torch(..., mesh_mapper=...)` with large tensors to an open mesh stalled:
once in a device-side tilize path, once as a host page-fault storm that froze `ps`. Converting on the host with
`device=None` and then `ttnn.to_device`, with torch preparation done before the mesh opens, avoided both.

**Isolating one chip.** On a p300c, one die alone is a custom topology: opening it needs `MeshShape(1, 1)` with
explicit physical device ids, and its own mesh descriptor when visibility is restricted. The two dies of a card
share a reset (see `tt-device-usage`).

**Watcher on multi-chip.** Watcher instrumentation can push the fabric router binary past the Ethernet kernel
config buffer at mesh open; `TT_METAL_WATCHER_NOINLINE=1` and a short polling interval let the run start.

**Expert placement.** Tensor-parallel experts and expert-parallel placement both appear on main; the faster one
depended on batch and on how evenly a step's experts spread over chips. See [moe](moe.md).
