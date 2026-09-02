---
name: tt-model-bringup-review
description: Reviews TTNN model code — layout and memory-config defaults, the decode residual contract, composite-op preference, program-config pitfalls, hidden host fallbacks, and logical batch versus tile padding. Use when reviewing model bringup or optimization changes under models/ or ttnn/ Python.
metadata:
  tier: model
  upstream:
    - repo: tenstorrent/tt-metal
      ref: d58cb341c703310cf41b5d88baafc0790ec0270b
      path: .agents/skills/optimize/SKILL.md
      branch: agentic-research/fast-models-fast
    - repo: tenstorrent/tt-metal
      ref: d58cb341c703310cf41b5d88baafc0790ec0270b
      path: .agents/skills/functional-decoder/SKILL.md
      branch: agentic-research/fast-models-fast
    - repo: tenstorrent/tt-buddy
      ref: ba9021417442d59756aa8cdf154a25648c9a0de5
      path: knowledge/matmul.md
---

# TTNN model bringup review

Assumes `tt-review-core`. Reviews model code that *calls* TTNN — bringup and optimization of
decoders and similar models. Op *implementation* is `ttnn-op-kernel-review`'s job.

## The residual stream is a contract, not a temporary

The decode residual memory config should carry through input RMSNorm, attention residual add,
post-attention RMSNorm, MLP input, and the final residual add, wherever the ops legally support it.

**A residual path that falls back to DRAM interleaved because it is convenient is a finding.** Where
a norm output or a residual add writes DRAM interleaved before the next norm or matmul, ask for a
sharded candidate or a precise blocker — the exact op, shape, memory config, and the error or PCC
failure that forced the fallback.

Layout conversions belong at the narrow API boundary only, with the residual contract restored
before the next residual add or norm. Look for `InterleavedToSharded` / `ShardedToInterleaved` pairs
whose only purpose is crossing a helper boundary, and ask whether the helper boundary is in the
right place.

## Topology before knobs

**Split Q, K, V matmuls consuming the same post-norm activation are a topology defect until proven
otherwise.** Before local program-config tuning, a packed QKV projection should have been tried. The
exception is a real per-projection contract — dtype, bias, adapter, normalisation, quantisation, or
device placement — that requires them to stay separate. *"The functional implementation was already
split"* is not a blocker.

For grouped-query attention the packed width comes from real head counts:
`q_heads * head_dim + 2 * kv_heads * head_dim`. Weight permutation conventions must be preserved —
**a fast packed path with wrong Q/K ordering is a correctness bug, not an optimisation.**

More generally: fewer larger matmuls usually beat more smaller ones, *when* the larger matmul
preserves dtype, layout, program-config quality, and downstream consumption. All four qualifiers
matter; a packing that wins on op count and loses on layout is not a win.

## Logical batch is not tile padding

Decoder kernels use tile-padded activation rows — one logical token padded to 32 rows. **Those
padded rows are a layout contract, not extra active users.**

Flag any change to logical batch, active user count, page-table semantics, or KV-cache indexing made
to satisfy an op shape check. A result with the right matmul shape and the wrong active batch is a
different workload, and it will pass PCC. Where a helper needs padded RoPE, current-position, or
head-layout metadata, the padded metadata must preserve the original logical batch semantics.

## Hidden host fallbacks

A helper that silently moves a tensor to host, or falls back to a host-side implementation, turns a
device path into a device-plus-host path. These do not show up as errors and rarely show up in the
diff — they show up as an unexplained gap between op time and wall time. When a diff adds a helper
call in a hot path, check what the helper does when its fast path is not legal.

## Composite ops

Prefer an existing composite op over a hand-rolled sequence — as a default, not a law. A hand-rolled
sequence can be right when the composite carries a layout change, an unwanted intermediate, or a
fidelity difference. Verify equivalence before recommending the swap, and phrase it as `CONSIDER`
unless you have.

## Program-config pitfalls

Do not assume the largest available core count is fastest. Logical program core count, activation
shard grid, `in0_block_w`, `per_core_N`, and output subblock fields are all part of the performance
contract, and more cores can shrink the legal K block enough to lose to a smaller coherent grid.

The producer's activation shard width is part of the consumer's program config: a norm path using
more cores makes each shard narrower, which can force `in0_block_w` down to 1 or 2 for the next
DRAM-sharded matmul. Treat a low `in0_block_w` on a dominant row as a symptom pointing *upstream*,
not as a local matmul property.

## Severity

Correctness — wrong Q/K ordering, changed logical batch, a broken KV-cache contract — is `MUST-FIX`.
Layout and topology findings are `SHOULD-FIX` with a named alternative. Anything resting on relative
performance without a measurement is `CONSIDER`; `tt-perf-claim-review` owns measured claims.
