# Performance advice for LLM decoders

Patterns that earlier optimize stages had to learn, numbered so evidence can cite them (OPT-001 and so on). The
optimize stage lists what each one requires as evidence; this page explains the pattern and how to try it. Matmul
configs, sparse matmul and precision basics are in [matmul-precision](matmul-precision.md).

## Code worth reading

- `tech_reports/LLMs/llms.md`, section 4: memory configs, matmul variants, DRAM-sharded matmuls, perf-report reading.
- `models/common/modules/attention/attention_1d.py`: BFP8 attention weights and KV cache, DRAM-sharded decode
  matmuls, SDPA configs, L1-sharded decode residual.
- `models/common/modules/mlp/mlp_1d.py`: decode/prefill MLP split, DRAM-sharded decode matmuls, precision knobs.
- `models/common/modules/lm_head/lm_head_1d.py` (`LMHead1D`) and
  `models/common/tests/modules/lm_head/test_lm_head_1d.py`: vocab-split LM head, DRAM-sharded weights, decode
  program config, expected construction and PCC checks.
- `models/common/sampling/generator.py` and `models/common/modules/sampling/sampling_1d.py`: on-device sampling.
- `ttnn/cpp/ttnn/operations/matmul/device/factory/matmul_multicore_reuse_mcast_dram_sharded_program_factory.cpp`,
  `ttnn/cpp/ttnn/operations/matmul/device/utilities/matmul_utilities.cpp` and
  `tests/ttnn/nightly/unit_tests/operations/matmul/test_matmul_dram_sharded.py`: Blackhole multi-reader
  DRAM-sharded matmul, its legality checks and tests.
- `models/demos/gpt_oss/tt/experts/` (README, decode, prefill, weights, config) and `models/demos/gpt_oss/tt/topk.py`:
  routed active-expert path with `ttnn.sparse_matmul`.
- `models/demos/gpt_oss/tt/`, `models/demos/gemma4/tt/`, `models/demos/deepseek_v3/tt/`: model-specific examples
  where common modules do not fit.

## General rules of thumb

- Data movement before math: a slightly smaller grid can beat a faster op if it avoids a reshard.
- Topology before knobs: count material matmuls, collectives, reshards and host/device crossings first. Fewer,
  larger matmuls usually beat more, smaller ones when dtype, layout, program config and downstream use survive.
- Decode activations usually stay width-sharded in L1 through norm, attention, residual, MLP and output projection;
  prefill activations usually sit in DRAM interleaved.
- SDPA, FlashDecode and FlashAttention ops beat hand-built attention when the model fits their contracts.
- Defaults for `memory_config`, `program_config` and `compute_kernel_config` are often correct but slow; set them
  for important ops. Shard specs and grids that divide dimensions into whole tiles avoid padding bugs and waste.

## OPT-001: Treat split decode QKV as a topology defect until proven otherwise

Separate Q, K and V matmuls on the same post-norm activation usually lose to one packed QKV projection; test the
packed form before tuning the split ones. It applies when the projections are plain linears of the same hidden
state with no per-projection op, dtype, bias, adapter, norm, quantization or placement that forces separation.

Pack the weights at load time, run one decode matmul of width `q_heads * head_dim + 2 * kv_heads * head_dim` (GQA),
then use the legal on-device QKV-head creation before rotary, cache update and SDPA. Keep any weight permutation
convention of the reference; a packed path with wrong Q/K ordering is a correctness bug. A good packed path shows
one QKV matmul row in `tt-perf-report` and no cluster of reshapes, slices and layout conversions that repair split
outputs. "The functional version was split" is not a reason to stay split. Packing can lose when it forces a
worse dtype, fidelity, layout, program config or extra output movement; compare under the same traced harness.

## OPT-002: Preserve the SDPA and KV-cache contract when changing attention

SDPA or FlashDecode time belongs to the attention contract. A change to QKV packing, head creation, rotary, cache
update, page tables, cache dtype or layout, or attention input memory config can move cost into SDPA, cache update
or layout conversion; compare the whole attention block at the same traced context, not just the projection row.

- BF16 cache: try the reduced cache dtype (usually BFP8) with the same context, page table, head layout and trace
  path. A functional baseline in BF16 is not evidence that BF16 is the optimized policy.
- A reduced cache that fails usually has a fill/update contract problem: fill tensors cast to the cache dtype,
  update inputs kept BF16/FLOAT32 ([prefill and KV](prefill-kv.md)). Also compare cache shape, local/global head
  mapping, page-table distribution and whether QKV packing changed the head creation path.
- Explicit SDPA decode config: when SDPA is material, measure an explicit config against the default. On Wormhole
  paged decode the usual first candidate is
  `ttnn.SDPAProgramConfig(compute_with_storage_grid_size=(8, 8), exp_approx_mode=False, q_chunk_size=0, k_chunk_size=0)`.
  On Blackhole do not copy it: the worker grid is 11 columns wide, an explicit 8-wide decode config read the wrong
  users once batch exceeded 8, and SDPA's BF16 running statistics make the chunk size set the long-context error.
  Match the grid to where the query shards live ([multichip](multichip.md), [numerics](numerics.md)).

## OPT-003: Preserve the decode residual layout through norms and residual adds

The residual stream is a layer-wide contract. Choose the decode residual memory config and carry it through input
norm, attention residual add, post-attention norm, MLP input and final residual add where the ops allow it. When
norms are material, use a sharded norm program config with sharded L1 input and output; a single-chip Wormhole
start is a width-sharded L1 residual over a clean grid with `ttnn.LayerNormShardedMultiCoreProgramConfig`, adapted
to the shape and hardware. Use complete rectangular grids ([memory](memory.md)). Pay layout conversions only at a
narrow helper boundary and restore the residual layout before the next add or norm.

## OPT-004: Sweep DRAM-sharded decode matmul geometry, not only dtype

The largest compute-core count is not automatically fastest: more cores can shrink the legal K block or per-core
output block. Logical core count, activation shard grid, `in0_block_w`, `per_core_N` and output subblocks are part
of the contract. Measure at least a candidate on the residual or intermediate shard grid, a larger-core candidate
when legal, and the existing helper or default. Hold dtype, fidelity, weight and input/output memory configs and
residual layout fixed while sweeping, unless a candidate needs a coherent layout change; then measure the layer.

The producer's shard width bounds `in0_block_w`; see [matmul-precision](matmul-precision.md#block-geometry) for
legal values and how to unlock larger ones. In one Llama 3.1 8B decode bringup `in0_block_w=16` beat `8` on the
material MLP rows. A divisibility error such as `(shard_shape[1] / tile_width) % in0_block_w != 0` is a reason to
try a wider upstream shard or inert padding, not a matmul-only blocker. A slightly slower norm can pay for a much
faster projection. For MLP and expert rows, run the geometry sweep under the best reduced-weight policy (usually
BFP4/LoFi) too, not only BFP8/HiFi2.

## OPT-005: Keep logical decode batch separate from tile padding

One logical token padded to 32 rows is a layout contract, not 32 users. Do not change logical batch, active users,
page-table semantics or cache indexing to satisfy a shape check; a run with the right matmul shape and the wrong
active batch is a different workload. If a helper needs padded RoPE, current-position or head metadata, pad the
metadata while keeping the logical batch. Compare SDPA only against a path with the same logical batch and context.

## OPT-006: Sign off cumulative optimized contracts, not isolated wins

Optimization is cumulative. Before a focused experiment, write down the best prior candidate's contracts:
projection topology, dtype and fidelity per tensor group, KV-cache dtype and layout, logical batch and padding,
attention config, residual and norm layout, dominant matmul geometry, required layout transitions. A focused run
may vary one of them; a run that fixes SDPA while losing MLP precision, or fixes geometry while changing logical
batch, is local evidence. Integrate old and new contracts in one candidate and remeasure. The next baseline is the
best cumulative correct candidate, not the latest focused run.

## OPT-007: Treat attention projection precision as its own decode search

Decode QKV and output projections are small-M, large-weight matmuls; BFP4 attention weights can pay as much as BFP4
MLP weights. Test attention weight dtype and fidelity independently of activation, KV-cache, CCL payload and MLP
dtypes. A BFP4 trial on a weaker topology (split Q/K/V, BF16 output) is screening only; remeasure on the packed,
fused or lowest-op-count topology the final decoder will use.

Reasons that are not reasons to reject: BFP4 MLP failed; BFP8 activations were chosen; a prefill row got slower
when decode is the target; a synthetic-weight check failed. Raw K/V cache PCC against a higher-precision run is a
diagnostic, because changing K/V projection precision legitimately changes the cached values; if layer output
passes but cache PCC fails, run a cache-consuming real-weight check (follow-on decode, multi-token replay,
top-k/readiness) before deciding. Real reasons: model-visible correctness loss, trace or generation failure,
latency loss including extra typecasts, or a precise op/shape/L1 blocker.

## OPT-008: Compare row-parallel output projection decompositions

Choose the algebra before the collective. The two main decompositions of a row-parallel output projection:
local-input/full-output matmul then reduce or reduce-scatter; and gathered-input/local-output matmul (often fused
all-gather plus matmul with output-sharded weights) then all-gather only if the residual needs replicated hidden.
They are different decompositions; a failed matmul+reduce-scatter does not reject the gathered-input family.

For fused all-gather plus output projection: repack the weight for local output width, adapt rank and layout to
the fused op, set the CCL axis, transfer count, memory configs and matmul program config explicitly; expect a fused
row whose N is the local output width. On an L1 circular-buffer failure, note requested and available bytes and try
the smallest legal local-output candidate. Fused CCL ops have hung or returned wrong values on 4-chip Blackhole
meshes and repaired versions often measured slower than separate ops ([multichip](multichip.md)); measure the
unfused path first and keep the faster correct one.

## OPT-009: Make repeated decode CCL buffers persistent when legal

Async CCL alone is not the end state for collectives that run every token or every layer (hidden all-gathers,
reduce-scatters after row-parallel matmuls, LM-head or logits collectives, expert collectives, fused CCL+matmul).
Try the API's persistent or preallocated output, intermediate and ring buffers. Hold algorithm, payload dtype,
dimension, topology, link count, workers and buffers per channel fixed while testing persistence. Async CCLs need
enough semaphores: the CCL helper classes in `tt_transformers`, `gpt_oss` and `deepseek_v3` track them; they also
need an L1-small region ([memory](memory.md)). Run watcher with async CCLs; mistakes show up as corruption or hangs.
Per-layer persistent buffers multiply by layer count; shared alternating slots served all layers in one model.

## OPT-010: Compare packed and split 3-matmul MLP gate/up in decode

Packed gate/up is a strong first candidate, not a proof. It must pay for splitting the packed output, the
activation, the binary combine and any layout movement before down; the doubled output width can force a worse
`per_core_N`, `in0_block_w`, subblock, L1 allocation or precision. Compare it against the best fused-elementwise
form of the split path (for SwiGLU the activation can fuse into the gate matmul or the following binary op), not an
untuned split baseline. If packed saves a little matmul time and adds more split, unary, binary or layout time,
the split path wins.

## OPT-011: Use phase-specific activation shards when they unlock matmul geometry

One residual or norm shard grid need not be the working input of every sub-block. When a material matmul group is
stuck at a small `in0_block_w` because the producer shard is narrow, reshard to a working shard with fewer cores and
wider shards just before the projections that share that input. Keep the model-visible boundary fixed; restore the
residual layout only where required (adds, norms, layer boundaries, cache helpers, collectives). Count the reshard
once across all projections that share it and compare the whole sub-block.

## OPT-012: Do not let synthetic precision stress veto real-weight wins

Random-weight and synthetic-activation checks (including tests named "representative semantics") are stress
probes. They catch crashes, shape bugs, trace replay bugs and explosions. When a cheaper policy passes real-weight
PCC, trace replay and runtime checks but fails a synthetic check, the synthetic distribution may be adversarial.
Either keep the faster policy or reproduce the failure on real weights under the disputed condition (non-aligned
lengths, paged prefill-to-decode, traced replay, larger batch, cache-consuming output, generation or readiness).
A test that marks the cheaper synthetic case as an expected failure while selecting the slower fallback is a
diagnostic, not an acceptance gate.

## OPT-013: Prove dtype policy reached the measured ops

Policy names, dataclass defaults, JSON summaries and constructor arguments are intent. Lazy loading, tensor caches,
rewrapping, helper defaults, a convenience `weight_dtype` argument or a replicated load path can silently turn a
claimed BFP4/LoFi policy into BF16 or BFP8 at the matmul. If the profiler row says `BF16 x BF16`, the matmul is not
using BFP4 weights. Debug the materialization path (cache key and directory, `LazyWeight` dtype, mesh mapper,
memory config, explicit `dtype=` on `ttnn.matmul`, a device tensor reused across policies) before timing.

## OPT-014: Cross precision with matmul geometry

Dtype/fidelity and block geometry interact. A geometry measured under BFP8/HiFi2 says nothing certain about
BFP4/LoFi, and one BFP4/LoFi geometry does not reject the others. More cores shrink the shard and the legal K
block; fewer, wider shards can win under reduced precision. Signs that a smaller-core or residual-grid candidate is
worth trying: a dominant row marked `SLOW`, low DRAM utilization, no output subblock, `in0_block_w<=2`, or a
recorded blocker on the larger grid. Do not mix dtype policies inside one geometry comparison.

## OPT-015: Sweep Blackhole DRAM readers per bank

On revisions with [#54242](https://github.com/tenstorrent/tt-metal/pull/54242),
`ttnn.MatmulMultiCoreReuseMultiCastDRAMShardedProgramConfig` accepts `num_workers_per_dram_bank=1`, `2` or `3`
(above one is Blackhole-only; the op rejects 4). Each extra worker reads and computes a disjoint slice of the same
bank shard through the same NOC0 endpoint: more reader/compute workers, not more DRAM channels.

```python
program_config = ttnn.MatmulMultiCoreReuseMultiCastDRAMShardedProgramConfig(
    in0_block_w=in0_block_w,
    per_core_M=per_core_M,
    per_core_N=per_core_N,
    num_workers_per_dram_bank=2,
)
```

It is a per-projection setting, not a new default: keep the chosen count in the model's per-role, per-SKU config and
do not add a shape allowlist to the generic op. Legality and screening:

- The per-bank weight shard width in tiles must divide by the reader count (a 56-tile shard allows two, not three).
- The device needs `dram_bank_count * reader_count` free worker cores; input-A storage cores are excluded. A
  placement failure makes the candidate illegal; it is not a reason to change the global default.
- Compute physical bytes per reader row from the stored tile format, block-float headers included. Small rows lose
  to request, multicast and sync overhead. At least 4 KiB per reader row and an input-A multicast grid of at least
  64 cores were useful screening heuristics in P150 sweeps, not correctness boundaries.
- More readers raise input-A multicast fanout. Narrow outputs, small weights, padding-only width or compute-bound
  kernels can be flat or slower. Three readers only when geometry and traffic justify them.
- Padding to make a count legal is an explicit, inert model tensor contract, sliced or masked before outputs.

Rank candidates under the production precision pairing (for LLM decode usually BFP8/HiFi2 and BFP4/LoFi) and check
the profiler row shows the intended dtype, fidelity and reader count. Measure in two layers: an isolated,
alternating-order 1/2/3-reader microbenchmark at the exact tensor and shard geometry (device time, physical and
logical GB/s, percent of SKU DRAM peak, PCC, run-to-run stability; for a flat or slow case, per-bank tiles,
per-reader row bytes, NOC burst splitting, multicast cores, math versus reader time); then the integrated traced
layer and full model. In the Llama 3.1 8B P150 reference integration, two readers on decode QKV, attention output,
gate/up and down raised batch-32 full-model decode from 32.75 to 42.08 t/s/u with flat TTFT; adjacent Llama and
Qwen shapes were flat or slower in broad sweeps. The count comes from same-shape evidence, with a one-reader control.

## LM head and sampling

- The LM head is a hidden-by-vocab decode matmul and usually DRAM-bound. `LMHead1D` is well optimized for 1D
  meshes; when it does not fit, use it as the template. Split over the mesh along vocab; keep the hidden stream
  sharded into final norm and LM head; DRAM-sharded or ring program configs; pad weights so the vocab-sharded
  matmul is legal and fast, keeping the real vocab size as separate metadata.
- Zero-padded LM-head columns produce zero logits that can beat negative real logits; mask IDs `>= vocab_size`
  with very negative values in each local shard before argmax or TopK.
- A non-power-of-two local TopK width can fall back to a slow single-core `TopKDeviceOperation`. Padding the shard
  to a power of two (very negative values, invalid indices) is a candidate, not a default: read the routing rule
  in the TopK op source first. On Blackhole a `k` above 64 routes to the large-indices composite regardless of
  width, and in one bringup that full-row composite beat every power-of-two chunking.
- Vocab-sharded greedy: local `topk(..., k=max_top_k)` (usually 32) per shard, all-gather the candidates, then
  semantically greedy params (`k=1`, `p=0`, `temp=1`). A physical per-shard `top_k=1` can produce a gathered width
  below one tile and fall back. Full-vocab all-gather plus global argmax is the slow path this avoids.
- Sampling trace keys differ for greedy, penalties and log-prob modes; warm and capture the active one.
