# MoE notes

Part of [optimize](../SKILL.md): detail moved out of the main skill file. Read only the sections in play.

- For dominant `ttnn.sparse_matmul` groups, keep routed active-expert execution and sweep legal `in0_block_w` and output-subblock candidates separately for each sparse projection role. Dense all-expert execution is a debug baseline, not an optimized result. The down path often has a different K/N shape and may need `is_input_a_sparse=True` when it consumes sparse expert activations. Also try input/output memory placement. Sparse weights may live in DRAM, but decode outputs and elementwise/reduction intermediates should stay in L1 or sharded L1 unless a measured L1-capacity blocker prevents it. Explicit `ttnn.DRAM_MEMORY_CONFIG` on decode intermediates is a performance smell; keep it only with a failed L1 attempt and before/after perf evidence. If `tt-perf-report` shows slow `BinaryNg`, `Unary`, `Slice`, `Reduce`, or other non-matmul ops around MoE, first check whether tensors are round-tripping through DRAM before retuning matmul configs. If one candidate hangs, wedges hardware, or fails PCC, reject that candidate, recover the device, and continue with other legal candidates instead of abandoning sparse program-config search.

## Sparse Matmul `nnz`

`ttnn.sparse_matmul(..., nnz=N)` selects a faster fixed-count path. The sparse sender skips zero sparsity entries, while receiver and compute kernels loop exactly `N` times. This is only correct when every invocation has exactly `N` non-zero entries in the sparsity tensor.

- Treat `nnz` as an exact invariant, not an upper bound. `top_k=4` does not prove `nnz=4`.
- Use explicit `nnz` when the sparsity tensor is constructed or validated so `count_nonzero(sparsity) == nnz` for every token and batch item. This avoids the runtime inference path and can matter: GPT-OSS Blackhole decode measured about 13-16% faster with a valid static `nnz` path than with runtime inference.
- Omit `nnz` when router weights, masking, dtype conversion, duplicate routes, expert remapping, or Blackhole zero-flush can change the actual non-zero count. The runtime inference path is slower but robust.
- A wrong `nnz` can hang or wedge the device. With watcher enabled it may fail loudly; without watcher it can look like a hardware hang.
- If you omit `nnz`, record the reason and the measured decode cost. If that cost is material, try to make the exact-count invariant true without changing model semantics, then remeasure.
- Background: [tt-metal #45943](https://github.com/tenstorrent/tt-metal/issues/45943) documents the deadlock contract, [#45958](https://github.com/tenstorrent/tt-metal/pull/45958) added asserts/docs, and [#45052](https://github.com/tenstorrent/tt-metal/issues/45052) / [#45969](https://github.com/tenstorrent/tt-metal/pull/45969) show the GPT-OSS Blackhole failure and runtime-inference workaround.
