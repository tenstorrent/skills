---
name: graph-fusing
description: Fuse a TTNN op graph into a faster numerically-equivalent one by (1) replacing a primitive op sequence with a dedicated tt-metal op, (2) simplifying/merging structural ops, and (3) folding adjacent ops into an existing op.
---

# Graph Fusing

Before using this workflow, follow the dependency and environment setup in
[the model-bringup entrypoint](../model-bringup/SKILL.md#startup).
Resolve its package root from this installed skill's location; do not assume
this plugin is copied into the target tt-metal checkout.

This is a correctness-preserving transform: every rewrite is proven numerically equivalent on device before it
lands ([Verify every rewrite](#verify-every-rewrite)). Read [fusion](../ttnn-knowledge/topics/fusion.md) before
step 1: where to find fused ops, and every pattern below in unfused and fused form. Fused paths can change
rounding, L1 use and collective behaviour: [numerics](../ttnn-knowledge/topics/numerics.md),
[memory](../ttnn-knowledge/topics/memory.md), [multi-chip and CCL](../ttnn-knowledge/topics/multichip.md).

## Steps

1. Search the tt-metal repo for ops that could replace parts of this graph (op library, golden functions, unit
   tests, and models that already wire them; the fusion topic lists where).
2. From the perf report and the code, write the full op sequence: a short table of each op with its inputs and
   the data movement (reshard, layout, collective) between them, so repeated ops, redundant movement and
   spelled-out math become visible.
3. Match the table against the patterns below and the ops found in step 1. Classify each candidate subgraph and
   apply the most promising one, in priority order: dedicated fused ops, then graph rewrites, then op merging.
4. Run the PCC equivalence test (unfused against fused) on device. Keep the rewrite only if PCC meets the
   prior-stage acceptance bar (default PCC >= 0.995) with no material regression, and the fused path is faster
   on the measured workload. A first failure is not a rejection: read the op's validation and adapt the inputs
   (layout, memory config, padding, program config) to its contract before giving up.
5. When the candidate is applied or rejected, return to step 2. Try every pattern that applies; stop only when
   nothing is left to try.

## Patterns, in priority order

### Dedicated fused ops (first)

The graph spells out the math of an op a tt-metal kernel already implements; collapse it into that op.

- Elementwise activation: `relu`, `relu6`, `hardsigmoid`, `silu`, `mish`, `gelu` → the dedicated activation op.
- Softmax: `exp / sum(exp)` → `ttnn.softmax`.
- RMSNorm: `mean(x²)·rsqrt·mul·weight` → `ttnn.rms_norm`.
- Distributed RMSNorm: `all_gather → rms_norm → slice` → `ttnn.rms_norm_pre_all_gather` + small-stats `all_gather` + `ttnn.rms_norm_post_all_gather`.
- SDPA: `matmul → scale → softmax → matmul` → `ttnn.transformer.scaled_dot_product_attention`.
- Split-QKV + split-heads: `slice ×3 → reshape → permute` → `ttnn.transformer.split_query_key_value_and_split_heads`.
- Create QKV heads (decode): the decode slice/reshape/permute → `ttnn.experimental.nlp_create_qkv_heads_decode`.
- Concat heads (decode): decode permute+reshape → `ttnn.experimental.nlp_concat_heads_decode`.
- Concatenate heads (prefill): `permute([0,2,1,3]) → reshape` → `ttnn.transformer.concatenate_heads`.
- RoPE: `x·cos + rotate_half(x)·sin` → `ttnn.experimental.rotary_embedding` (rotate-half) or `rotary_embedding_llama` (interleaved); decode via `token_index`.
- TopK: `sort → slice[:k]` → `ttnn.topk`.

### Graph rewrites (second)

Remove ops or data movement by algebraic identity, or merge peer ops that share an input.

- RepVGG conv-sum: `conv3x3(x) + conv1x1(x)` → one `conv3x3` with `w3 + pad(w1)` (weights combined at load time).
- Shared-LHS matmul: two or more matmuls on the same input → one matmul over the concatenated weights, then
  split (QKV, gate/up). Keep it only if it beats a tuned separate path
  ([packing same-input matmuls](../ttnn-knowledge/topics/matmul-precision.md#packing-same-input-matmuls)).
- Spatial mean: `mean(x, [1,2])` → `reshape → mean(dim=2)`.
- Permute-reshape-permute that composes to an identity ordering → a single `reshape`.

### Op merging (third)

An anchor op stays; a neighbour folds into it through an argument or config.

- Conv + bias → `ttnn.conv2d(..., bias_tensor=b)`.
- Conv + multiply/scale → per-channel scale folded into conv weights at load time.
- Conv2d + activation → `Conv2dConfig(activation=...)`.
- Matmul/Linear + activation → `ttnn.matmul(..., activation="silu")`.
- Input-arg activation → eltwise binary → `ttnn.add(x, y, input_tensor_a_activations=[ttnn.UnaryOpType.RELU])`.
- Matmul + bias → `ttnn.linear(a, b, bias=bias)`.
- Permute/transpose + matmul → `ttnn.matmul(a, b, transpose_b=True)`.
- Slice after matmul → slice pushed into the operand (narrower matmul).
- BatchNorm (inference) + conv → affine folded into conv weights and bias at load time.
- Pad + pooling/conv → pad folded into the consumer's padding attribute.
- Numeric-stable softmax: `softmax(x − max(x))` → `ttnn.softmax(x, numeric_stable=True)`.
- Reduction + reshape: `reduce(keepdim=False) → reshape` → `reduce(keepdim=True)`.
- Scaled-sum → mean: `sum(x)·(1/N)` → `ttnn.mean(x)`.
- RoPE decode / decode-reshape: decode layout `permute`/`reshape` folded into decode-mode rotary embedding (`token_index=0`).

## Verify every rewrite

A rewrite is done only when an on-device PCC equivalence test of the changed path shows accuracy is not degraded
and the measured fused path is faster.
