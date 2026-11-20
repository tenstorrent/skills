# Mixture of experts

A routed layer behaves differently at every batch: at batch 1 it is a small dense layer, at batch 32 it touches
many experts with few tokens each, in prefill it touches most experts with many tokens each. Plan and measure
it per batch, never from one layer at batch 1.

- **Roofline per batch.** Distinct experts touched per step ≈ E·(1 − (1 − k/E)^B) for E experts and top-k, times
  bytes per expert, plus dispatch and combine traffic. Targets come from this, not from the dense formula.
- **Real routing.** Record router decisions in the HF captures and drive performance tests with them, so hot
  experts and load imbalance are in the measurement. Uniform random routing hides both.
- **Layout by measurement.** Expert-parallel, tensor-parallel or a mix, chosen on full-model step time at batch 1
  and the largest batch, including the collectives each implies.
- **Kernels.** Existing ttnn MoE ops first. A fused decode path (routing, gather, expert matmuls, combine) is the
  usual next step when dispatch and small-expert overhead dominate at large batch; see [ops](ops.md).
- **Precision per layer.** Expert layers differ in how much low precision they tolerate; choose per layer group
  on the reference tiers.
- **Record** in `model.json` `moe`: experts, top-k, layout, distinct experts per step and decode time against
  its floor at batch 1 and the largest batch.
- **Hangs.** Multicast and routing kernels can hang only under real traffic; replay recorded requests at the
  largest batch before serving.
