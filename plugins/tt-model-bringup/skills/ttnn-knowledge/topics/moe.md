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
`nnz` must match the actual non-zero count exactly or it can hang (see `optimize`, sparse matmul).

**Reference implementation:** `models/demos/gpt_oss/tt/experts/` (decode, prefill, weights, config) and
`models/demos/gpt_oss/tt/topk.py`.
