# Matmul choices and precision

How to pick matmul program configs, sparse matmul settings, dtypes, math fidelity and compute kernel configs. The
optimize stage says which of these trials are required; this page says how to run them. The longer per-pattern
notes are in [perf-advice](perf-advice.md).

## Program configs by phase

- Decode matmuls (small activation, large weight) are usually DRAM-bound: use
  `ttnn.MatmulMultiCoreReuseMultiCastDRAMShardedProgramConfig`, with weights width-sharded in DRAM and activations
  and outputs width-sharded in L1 on the matching core grid. It is decode-only: its validation rejects an
  activation taller than one tile row (`M == 1` tile), so no prefill chunk can use it. `num_workers_per_dram_bank`
  accepts 1 to 3 (the op rejects 4); see [OPT-015](perf-advice.md#opt-015-sweep-blackhole-dram-readers-per-bank).
- Prefill matmuls with large M and N are usually compute-bound: `ttnn.MatmulMultiCoreReuseMultiCastProgramConfig`
  over a large 2D grid, activations DRAM interleaved. Short chunks and tails can be bandwidth- or dispatch-bound
  and use the 1D multicast or 2D configs over interleaved or width-sharded weights. The best decode config is
  rarely the best prefill config.
- If `tt-perf-report` says a one-tile-row decode matmul is DRAM-bound and it is not DRAM-sharded, try the
  DRAM-sharded config; a reshard usually makes it legal and it usually wins.
- For non-DRAM-sharded matmuls, use the largest clean core grid the shape allows. With the DRAM-sharded config the
  compute core count is fixed by the op, so the input and output shard grids are freer than they first look.
- `ttnn.experimental.minimal_matmul` is a candidate on revisions that have it. The
  [DiT linear](https://github.com/tenstorrent/tt-metal/blob/fc7777ceca4a2360667dbb193b28fedcccc89866/models/tt_dit/layers/linear.py)
  and [matmul config helper](https://github.com/tenstorrent/tt-metal/blob/fc7777ceca4a2360667dbb193b28fedcccc89866/models/tt_dit/utils/matmul.py)
  derive `MinimalMatmulConfig` from padded M/K/N and the grid. A DiT result is not evidence for an LLM shape;
  compare it with the tuned existing kernel at the target shapes. When profiler advice reports a missing
  `program_config` on this op, look at its `config=` argument.
- Prefill kernel winners differ by row count: one chunk size does not choose the kernel for another. Check the
  selected kernel before trusting a generic utilization estimate; the report can confuse activation storage cores
  with DRAM reader or compute cores.
- Observed in a batched-prefill bringup, not a documented limit: a dense pass with a wide output (N above about
  8192) overflowed L1 in one 2D block above 512 rows; running it in row pieces through the batch dimension (1D
  config with `fuse_batch` off) fit.

## Block geometry

- `in0_block_w` must divide the tiled K dimension and the input shard's K-tile width. Values need not be powers of
  two (3, 5, 7, 10, 14 can be legal). Larger is usually faster for dominant DRAM-sharded rows; `2` is a floor,
  and stopping at 4 or 8 out of habit leaves time on the table.
- If only small values are legal, the producer shard is too narrow: a working shard with fewer cores and wider
  shards, or a different shard-spec core count, often unlocks larger divisors (see
  [OPT-011](perf-advice.md#opt-011-use-phase-specific-activation-shards-when-they-unlock-matmul-geometry)).
  Changing the shard spec is usually better than padding when it keeps the surrounding layout.
- Padding weights or hidden/intermediate channels can enable a better grid or block. Padded channels must be
  mathematically inert: sliced, masked or reduced away before they affect outputs. Pad at weight-load time or on
  device, not with torch in the runtime path.
- Output subblocks of at least `2x1` or `1x2` when legal; larger is often better; read the matmul validation code
  for the legal set.
- When an op runs out of L1, first try more cores; if that is impossible, reduce `in0_block_w`, `out_subblock_h` or
  `out_subblock_w` and keep the combination that loses least. Geometry tuned for one dtype, batch, grid or KV dtype
  usually stops fitting when any of them changes; the overlap message gives the byte endpoints ([memory](memory.md)).
- On larger-core devices, do not hard-code a Wormhole-shaped 8x4 or 32-core decode layout. Sweep grids as one
  coherent layout (residual and intermediate memory configs, norm program grids, matmul program configs and shard
  specs agree). A first "shard grid does not fit program grid" error means adapt the contract, not reject the grid.
  Blackhole's 11-column grid: [multichip](multichip.md).

## Packing same-input matmuls

When several matmuls consume the same activation (Q/K/V, gate/up, model-specific groups split for code
convenience), a packed projection (weights concatenated at load time, one matmul, split on device) is the first
candidate. It loses when it forces a worse dtype, fidelity, layout or program config, or when the split,
activation, binary and layout work after it costs more than the saved launch. Details:
[OPT-001](perf-advice.md#opt-001-treat-split-decode-qkv-as-a-topology-defect-until-proven-otherwise) and
[OPT-010](perf-advice.md#opt-010-compare-packed-and-split-3-matmul-mlp-gateup-in-decode).

## Sparse matmul `nnz`

`ttnn.sparse_matmul(..., nnz=N)` selects a fixed-count path: the sparse sender skips zero sparsity entries, while
receiver and compute kernels loop exactly `N` times. It is correct only when every invocation has exactly `N`
non-zero entries.

- `nnz` is an exact invariant, not an upper bound; `top_k=4` does not prove `nnz=4`.
- Pass it when the sparsity tensor is built or validated so `count_nonzero(sparsity) == nnz` for every token and
  batch item. GPT-OSS Blackhole decode measured about 13-16% faster with a valid static `nnz` than with runtime
  inference ([#45052](https://github.com/tenstorrent/tt-metal/issues/45052),
  [#45969](https://github.com/tenstorrent/tt-metal/pull/45969)).
- Omit it when router weights, masking, dtype conversion, duplicate routes, expert remapping or Blackhole
  zero-flush can change the count; runtime inference is slower but robust.
- A wrong `nnz` can hang or wedge the device; with watcher it may fail loudly, without it it looks like a
  hardware hang ([#45943](https://github.com/tenstorrent/tt-metal/issues/45943) documents the deadlock contract,
  [#45958](https://github.com/tenstorrent/tt-metal/pull/45958) added asserts).
- Sparse projection roles (gate/up and down) have different K/N; tune `in0_block_w` and output subblocks per role.
  The down path may need `is_input_a_sparse=True` when it consumes sparse expert activations. Other MoE notes:
  [moe](moe.md).

## Precision and fidelity

- Starting point when no earlier policy exists: BF16 activations and norms, BFP8 attention and MLP weights, BFP8
  KV cache if accuracy allows, selective BFP4 trials for MLP or expert weights. Move one tensor group at a time
  (attention weights, MLP/expert weights, KV cache, activations/residuals, CCL payloads, norms, logits, layer
  exceptions) so a regression has one owner.
- MLP gate/up (FF1/FF3) often tolerates BFP4; down (FF2) is more sensitive. Router logits, top-k and routing
  weights are more sensitive still (see [datatype-sweep](../../datatype-sweep/SKILL.md)).
- Fidelity is a separate knob from dtype. BFP8 with HiFi2 is a safe start, not the answer: BFP8 with LoFi can be
  materially faster and still pass model-level accuracy. BFP4 is normally paired with LoFi. LoFi truncates
  operands, and PCC can miss the resulting gain error; add a gain check ([numerics](numerics.md)).
- BF16 weights or numerically sensitive ops: HiFi4 or FP32 accumulation when accuracy evidence calls for it.
- KV cache: the prefill fill and decode update paths have different dtype contracts ([prefill and KV](prefill-kv.md)).
  A BFP8 cache can pass a long-prompt layer check and fail on short prefixes, where few cached rows carry the
  attention; in one Qwen3-8B bringup the per-user PCC on a short prefix was 0.9942 with BFP8 against 0.9993 with
  BF16 on the same inputs.
- Activation dtype mostly matters through CCL and layout traffic. A global BFP8-activation switch is not a sweep:
  attention activations, CCL payloads, MLP or expert activations, residual and norm tensors and logits can differ.
  If global BFP8 fails, find which path fails rather than reverting everything to BF16.
- A dtype trial under one residual/collective topology says little about another; cross precision with topology
  and geometry ([OPT-014](perf-advice.md#opt-014-cross-precision-with-matmul-geometry)).
- A small PCC drop after lowering precision can be real precision loss. A collapse (PCC near zero, magnitude
  explosion, NaN, roughly half-scale output) is more often a dtype-propagation, layout, cache, scale or harness bug:
  reproduce it with a minimal same-contract test and compare intermediates with the higher-precision path before
  calling the dtype invalid. A claimed policy can also fail to reach the op
  ([OPT-013](perf-advice.md#opt-013-prove-dtype-policy-reached-the-measured-ops)).
- Synthetic or random-weight checks catch crashes and shape bugs; they do not decide a precision policy
  ([OPT-012](perf-advice.md#opt-012-do-not-let-synthetic-precision-stress-veto-real-weight-wins)).

## Compute kernel configs

Wormhole-style starting points (the checkout's compute-kernel config class for the target architecture takes the
same fields):

```python
compute_kernel_config_lofi = ttnn.WormholeComputeKernelConfig(
    math_fidelity=ttnn.MathFidelity.LoFi,
    math_approx_mode=False,
    fp32_dest_acc_en=False,
    packer_l1_acc=True,
)

compute_kernel_config_hifi2_fp16 = ttnn.WormholeComputeKernelConfig(
    math_fidelity=ttnn.MathFidelity.HiFi2,
    math_approx_mode=False,
    fp32_dest_acc_en=False,
    packer_l1_acc=True,
)

compute_kernel_config_hifi4 = ttnn.WormholeComputeKernelConfig(
    math_fidelity=ttnn.MathFidelity.HiFi4,
    math_approx_mode=False,
    fp32_dest_acc_en=True,
    packer_l1_acc=True,
)
```

`models/common/tensor_utils.py` serializes program and compute-kernel configs for reports.
