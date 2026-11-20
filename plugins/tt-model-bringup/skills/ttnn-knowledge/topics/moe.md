# Mixture of experts

**A routed layer is a different problem at every batch.** At batch 1 it is a small dense layer; at large batch it
touches many experts with few tokens each; prefill touches most experts with many tokens. One port that ran
experts per user served about 2 tokens/s per user at batch 32; processing all users' selections in one pass made
it tens of times faster. Plan and measure each batch, not only batch 1.

**Random routing hides real costs.** Benchmarks with random tokens understated decode time: real routing touched
uneven expert sets, and when expert work split evenly over all cores, every core read the same DRAM banks at the
same moment (staggering bank start fixed it, bit-identically). A fused MoE decode kernel hung only under real
traffic (a large multicast from one core); random-token tests passed. Recording real router decisions and
replaying real requests at the largest batch found both.

**Batch invariance.** A grouped expert op gave each user slightly different results depending on the other users
in the batch; PCC stayed at 1.0 while later layers picked different experts. Compare batched against alone on
generated tokens for MoE paths.

**Expert parallel vs tensor parallel.** Expert-parallel placement was slower at batch 1 when one token's experts
landed unevenly on chips (the busiest chip reads more than its share), and faster at large batch. Some efforts
kept tensor-parallel experts for batch 1 and a separate path for large batch.

**Gathers with padding.** Padding indices that all point at one zero row serialize thousands of reads on one DRAM
row; spreading them over many zero rows removed the bottleneck.

**Folded weights.** Folding a norm's gamma into merged expert weights and then feeding an already scaled input
applied gamma twice; numerics failed only at the fused path.

**Op limits seen:** `moe_compute` handled BFP4 experts but gave NaN with BFP8; `ttnn.sparse_matmul` with a static
`nnz` must match the actual non-zero count exactly or it can hang (see [matmul-precision](matmul-precision.md#sparse-matmul-nnz)).

**Tuning the active-expert path.** Dense all-expert execution is a debug baseline. Tune each sparse projection
role separately ([matmul-precision](matmul-precision.md#sparse-matmul-nnz)). Sparse weights may live in DRAM, but
decode outputs and elementwise or reduction intermediates belong in L1 or sharded L1 unless L1 capacity is shown
to block it; an explicit `ttnn.DRAM_MEMORY_CONFIG` on a decode intermediate is a smell. Slow `BinaryNg`, `Unary`,
`Slice` or `Reduce` rows around MoE usually mean tensors round-trip through DRAM; check that before retuning
matmuls. A candidate that hangs or fails PCC is rejected on its own; the rest of the sweep continues after device
recovery ([tt-device-usage](../../tt-device-usage/SKILL.md)).

**Reference implementation:** `models/demos/gpt_oss/tt/experts/` (README, decode, prefill, weights, config),
`models/demos/gpt_oss/tt/topk.py` and `models/demos/gpt_oss/tests/unit/test_modules.py`. The pattern: router top-k
scores as a sparsity tensor, `ttnn.sparse_matmul` for the gate/up/down active-expert projections, score weighting,
`ttnn.sum` or the model's reduce over experts, and only then any TP or EP collective. Galaxy-only fused MoE paths
need evidence that the target hardware and ops support them.

## Experts on a mesh

Placement options: tensor-parallel experts (each active expert split over the devices), expert-parallel placement
(each device owns some experts), 2D TP, expert replication across device groups, or a hybrid. Which is faster
depends on batch and routing spread (above), so the choice is measured on full-model step time.

Expert mapping, routing-weight layout, shared-expert placement, sparse weight layout, DRAM/L1 memory configs,
semaphores and preallocated buffers, and the final residual layout are correctness contracts. Gate and expert MLPs
passing separately is not enough: validate router, sparse expert projection, score weighting, expert reduce and
the TP/EP collective as one sequence.

On Galaxy 4x8, GPT-OSS throughput experts (`models/demos/gpt_oss/tt/experts_throughput/`: config, weights,
decode, fused_decode) dispatch tokens to expert owners along one axis and use the other axis as replicated expert
groups or TP groups for single-user throughput. Replication multiplies expert memory: it fits only when a memory
plan for all layers plus the full KV cache at the target maximum sequence length shows it, not one decoder layer.
