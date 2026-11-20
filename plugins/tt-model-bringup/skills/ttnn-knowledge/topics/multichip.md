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

## Parallelization patterns

Read `tech_reports/LLMs/llms.md` section 3.3 (multi-device) first; it does not cover expert parallelism or MoE
(see [MoE](moe.md)).

**Code worth reading.** The GPT-OSS structure and the `models/common` modules are the first references:
- `models/demos/gpt_oss/config.py`: `ModeConfig`, `MeshConfig`, mapper helpers (`column_parallel`, `row_parallel`,
  `sequence_parallel`) and CCL helpers; `MeshConfig` is a good shape for a 2D plan.
- `models/demos/gpt_oss/tt/ccl.py`: a small generic `CCLManager` with ping-pong semaphores.
- `models/demos/gpt_oss/tests/test_factory.py`: mesh setup, fabric config, hardware-shape parametrization.
- `models/common/modules/attention/attention_1d.py`: local heads, paged KV cache, optional fused all-gather plus WO
  matmul, reduce-scatter output.
- `models/common/modules/mlp/mlp_1d.py`: W1/W3 output sharding, W2 input sharding, reduce-scatter output.
- `models/common/modules/rmsnorm/rmsnorm_1d.py` and `rmsnorm_2d.py`: local and distributed-stats norms.
- `models/common/modules/tt_ccl.py`: CCL semaphores, topology detection, link-count helpers.

When they do not fit the model, model-local code in the same style (config dataclasses, explicit mesh helpers,
setup-time weight conversion, straight-line runtime forwards) keeps the contracts that matter: local head
ownership, sharded residuals, distributed norms, fused all-gather plus output projection, reduce-scatter outputs,
persistent CCL buffers, setup-time weight transformation.

**Dense 1D tensor parallel (meshes up to 8 chips).** The common pattern:
- WQKV column (output) sharded, so each device owns local Q/K/V heads; per-device local-KV-head cache, paged if
  the baseline is; SDPA on local heads.
- WO row (input) sharded over concatenated local heads, then reduce-scatter or all-reduce to restore the residual
  layout.
- W1/W3 column sharded over the intermediate dimension; W2 row sharded over it, then reduce-scatter or all-reduce.

A replicated residual (all-reduce) simplifies correctness; a sharded residual can remove or shrink collectives
across the stack. Each decoder layer's output layout must match its input layout. When a sharded residual is hard
to wire, a small shape-faithful probe of the producing op and the next consumer (norm, residual add, attention or
MLP input) shows whether the reduced layout can be consumed directly; "fused matmul plus reduce-scatter runs" or
"it is slow when followed by an immediate all-gather" does not answer that. Output-projection decompositions:
[OPT-008](perf-advice.md#opt-008-compare-row-parallel-output-projection-decompositions).

**Galaxy as evidence, not template.** `models/demos/llama3_70b_galaxy/` (`tt/model_config.py`,
`tt/llama_attention.py`, `tt/llama_mlp.py`, `tt/llama_ccl.py`, `tt/distributed_norm.py`) shows where collectives
belong in an optimized 2D decoder: attention splits Q/K/V over devices, runs SDPA on local heads, gathers heads
before WO and reduces to restore the residual; the MLP works on sharded intermediate chunks, gathers when W2 needs
the full intermediate, then reduces. Do not copy its custom `llama_rs_*` ops as a baseline, 32-chip asserts,
prefetcher-specific control flow, environment-variable topology switches or hardcoded memory configs.

**2D meshes (Galaxy 4x8).** Flattening to TP=8 ignores the second axis. A 2D plan names which axis owns TP, expert
parallelism, sequence parallelism or replication; which collectives cross rows and which cross columns; memory for
activations, weights, experts and KV cache with all layers loaded; communication volume against the 1D
alternative; and why it should improve single-user latency.

**Distributed RMSNorm.** When hidden activations are sharded over the normalized dimension, a local norm computes
wrong statistics. The primitive sequence is `ttnn.rms_norm_pre_all_gather`, an all-gather of the stats
(`ttnn.experimental.all_gather_async` or `ttnn.all_gather`), then `ttnn.rms_norm_post_all_gather`. Implementations:
`models/common/modules/rmsnorm/rmsnorm_1d.py::_prefill_1d_distributed`, `rmsnorm_2d.py::decode_forward` and
`::prefill_forward`, `models/demos/deepseek_v3/tt/rms_norm/distributed_rms_norm.py::DistributedRMSNorm`,
`models/tt_transformers/tt/ccl.py::tt_distributed_rmsnorm` and `::tt_sharded_distributed_rmsnorm`. A replicated
residual with a local norm is also correct, and can be faster if it keeps the layer chain's layout.

## Validating a multi-chip path

Compare against the single-chip TTNN baseline with identical weights, inputs, page tables and positions; this
separates sharding and collective bugs from HF-versus-TTNN differences. When PCC sits near the threshold, compare
components: input norm, QKV projection, RoPE, SDPA output, WO projection and reduction, post-attention residual
and norm, router and top-k for MoE, active expert outputs, W2/down projection and reduction, final residual.

Check layouts and collectives before precision. The usual bugs: wrong sharding, wrong gather or reduce axis, bad
padding or slicing, a bias applied once per TP shard before an all-reduce, wrong local head count, wrong local
KV-cache shape, mismatched input and output residual layout.

## Runtime setup

- Configure the fabric that matches the mesh and topology before opening a mesh that runs CCLs: repo pytest
  `device_params`, or `ttnn.set_fabric_config(...)` before `ttnn.open_mesh_device(...)`. For example, 8-chip
  Wormhole T3K 1D TP uses `FABRIC_1D_RING` with `ttnn.Topology.Ring`; check what the target system's tests use.
- A CCL failure on a mesh opened without the matching fabric config is setup evidence, not hardware evidence,
  until it reproduces with the right config and CCL topology.
- CCL semaphore ownership is explicit; the generic managers above usually suffice. Async CCLs need enough
  semaphores and an L1-small region ([memory](memory.md)); run watcher with them, since mistakes show as corruption
  or hangs. Watcher and profiler go in separate runs ([profiling](profiling.md)).
- Pad CCL-sensitive hidden dimensions deliberately and slice at a documented boundary.
- After a failed multi-chip run, recovery follows [tt-device-usage](../../tt-device-usage/SKILL.md).
