# Memory and L1

L1 and circular-buffer failures: why a dtype, batch, grid or KV change breaks a working config, modules that collide
only when combined, collective workspaces and long-context budgets.

**"Static circular-buffer end overlaps live L1."** The most common device failure across bringups. A program's
static CB region ends above the lowest live L1 tensor. The message gives both addresses to the byte, and the CB
sizes of the op's factory account for the difference exactly, so arithmetic on the factory finds the cause faster
than trial runs.

**Explicit geometry is tuned for one configuration.** Matmul blocks, SDPA chunk sizes, norm grids and subblocks
chosen for BFP8 weights at one batch stop fitting when weights or activations widen (BF16, FP32 accumulation),
the batch grows, the KV dtype changes or the grid changes. Expect to re-derive them for every precision or batch
candidate, per op role and phase, rather than reuse the tuned values. A candidate that fails on L1 may be
rejected for the wrong reason if this is skipped.

**Modules collide only when combined.** Each module's tests pass alone while the full model fails, because
persistent L1 buffers of one module (decode scratch kept per layer, RoPE rows, collective workspaces) overlap
transient CBs of another, often only at some prompt lengths. What helped: one map of persistent on-chip and DRAM
allocations per module, scratch shared across layers instead of per layer, persistent buffers allocated before
any program-local CBs, and a full-model run with allocation tracking.

**Collective workspaces scale with layers.** Per-layer, per-shape persistent scratch for all-reduce or all-gather
multiplies by the layer count; two shared alternating slots owned at mesh level served all layers.

**Async collectives need L1-small space.** Without a reserved L1-small region, async reduce-scatter allocated its
semaphores in L1 at run time (logged as "Allocating semaphores in L1") and deadlocked across dies on long
prefill. Opening the device with `l1_small_size=24576` removed the warnings and the hang.

**Sharded norms.** A sharded norm accepted a non-rectangular width-shard grid, but its reduction workers wrapped
the bounding rectangle and read holes (outputs near 3e38). Use complete rectangular grids. With FP32 accumulation
the sharded layernorm factory allocates large FP32 scratch per core; size the grid for it.

**Long-context budgets.** KV for many users at long context sits at the edge of device DRAM, and chunked prefill
briefly needs extra on top. Budget on paper (weights, KV pool, prefill transient, trace region) before the first
warmup; the first warmup is a slow way to find out.
