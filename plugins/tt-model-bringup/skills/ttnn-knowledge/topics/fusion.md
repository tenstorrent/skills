# Graph fusion: rewrite kinds and worked examples

How to turn a TTNN op graph into a faster equivalent one. The [graph-fusing](../../graph-fusing/SKILL.md) stage
lists the patterns it must assess; this page explains where to find fused ops and shows each pattern in
unfused and fused form. Every rewrite is verified on device; fused paths can change rounding
([numerics](numerics.md)), L1 use ([memory](memory.md)) and, for collectives, correctness
([multichip](multichip.md)).

## Finding a dedicated op

A dedicated kernel does in one dispatch, with fused compute and tuned data movement, what a spelled-out primitive
sequence does in many; most fusion wins come from recognizing that a subgraph already is an op. It is still a
candidate: measure it, because a fused op can force a worse layout, program config or precision.

Find the op and its contract:
- `ttnn/cpp/ttnn/operations/**`: the op library; grep for the operation name and for the math it does.
- `ttnn/ttnn/operations/*.py`: Python bindings and golden functions (`attach_golden_function`); the golden is the
  exact math the op computes, so it tells you which primitive sequence the op replaces.
- `tests/ttnn/**`: unit tests give the signature, valid shapes, dtypes and layout constraints.

Find idiomatic usage and hand-fused implementations in models:
- `models/common/modules/`: reusable, tuned blocks (`attention/`, `mlp/`, `lm_head/`, `rmsnorm/`, `moe/`, `rope/`,
  `sampling/`). Prefer these before hand-rolling a sequence.
- `models/demos/gpt_oss/tt/`: MoE experts on `ttnn.sparse_matmul`, `topk.py`, attention and CCL wiring.
- `models/demos/deepseek_v3/tt/` and `models/demos/deepseek_v3_b1/micro_ops/`: MLA attention, MoE gate, LM head,
  and custom fused micro-ops (`create_q_heads`, `dram_streaming_matmul`, `deepseek_moe_gate`, fused CCL variants).
  Fused CCL variants carry the Blackhole caveat in [multichip](multichip.md).
- `models/tt_dit/`: DiT and video models (for example Wan): wiring in `models/tt_dit/models/transformers/wan2_2/`,
  shared ops in `models/tt_dit/layers/` (`conv2d`, `conv3d`, `linear`, `normalization`, `feedforward`) and
  `models/tt_dit/blocks/` (`attention`, `transformer_block`).
- Other `models/demos/**` and `models/experimental/**`: when the target resembles a model family, its `tt/` code
  shows the fused op calls and shape conventions. More paths: [reference code](reference-code.md).

When a fused op rejects the case, read its validation and adapt the inputs (layout, memory config, shape padding,
program config) to its contract; an extra layout change in front of it can be enough.

## The three kinds

- **Dedicated fused ops.** The graph spells out the math of an op that a tt-metal kernel already implements;
  collapse the sequence into that op.
- **Graph rewrites.** No new kernel: remove ops or data movement by algebraic identity, or merge peer ops that
  share an input. Merging same-input matmuls (QKV, gate/up) is a candidate, not a rule: it can lose when the
  wider output forces worse geometry or the split after it costs more than the saved launch
  ([matmul-precision](matmul-precision.md#packing-same-input-matmuls)).
- **Op merging.** An anchor op stays; a neighbour (bias, activation, scale, transpose, pad, reshape, max-subtract)
  folds into it through an argument or config. Usually a small win, but it removes a dispatch.

## Worked examples: dedicated fused ops

```python
# activation (SiLU)
# unfused
s = ttnn.sigmoid(x)
out = ttnn.multiply(x, s)
# fused
out = ttnn.silu(x)

# softmax
# unfused
e = ttnn.exp(x)
denom = ttnn.sum(e, dim=-1, keepdim=True)
out = ttnn.div(e, denom)
# fused
out = ttnn.softmax(x, dim=-1)

# rmsnorm
# unfused
sq = ttnn.multiply(x, x)
var = ttnn.mean(sq, dim=-1, keepdim=True)
inv = ttnn.rsqrt(ttnn.add(var, eps))
normed = ttnn.multiply(x, inv)
out = ttnn.multiply(normed, weight)
# fused
out = ttnn.rms_norm(x, epsilon=eps, weight=weight)

# distributed rmsnorm (mesh; x_shard sharded on dim 3 across the mesh)
# unfused
g = ttnn.all_gather(x_shard, dim=3)
sq = ttnn.multiply(g, g)
var = ttnn.mean(sq, dim=-1, keepdim=True)
inv = ttnn.rsqrt(ttnn.add(var, eps))
normed = ttnn.multiply(ttnn.multiply(g, inv), weight)
out = ttnn.mesh_partition(normed, dim=3, cluster_axis=1)   # re-shard back per device
# fused (only the small stats cross the mesh; reference implementations in multichip.md)
stats = ttnn.rms_norm_pre_all_gather(x_shard)
stats = ttnn.all_gather(stats)
out = ttnn.rms_norm_post_all_gather(x_shard, stats, weight=weight_shard)

# sdpa
# unfused
k_t = ttnn.permute(k, (0, 1, 3, 2))
scores = ttnn.matmul(q, k_t)
scores = ttnn.multiply(scores, scale)
probs = ttnn.softmax(scores, dim=-1)
out = ttnn.matmul(probs, v)
# fused
out = ttnn.transformer.scaled_dot_product_attention(q, k, v, is_causal=False, scale=scale)

# split-qkv + split-heads (qkv: [b, s, 3*h*d])
# unfused
hidden = h * d
q = ttnn.slice(qkv, (0, 0, 0), (b, s, hidden))
k = ttnn.slice(qkv, (0, 0, hidden), (b, s, 2 * hidden))
v = ttnn.slice(qkv, (0, 0, 2 * hidden), (b, s, 3 * hidden))
q = ttnn.permute(ttnn.reshape(q, [b, s, h, d]), (0, 2, 1, 3))
k = ttnn.permute(ttnn.reshape(k, [b, s, h, d]), (0, 2, 1, 3))
v = ttnn.permute(ttnn.reshape(v, [b, s, h, d]), (0, 2, 1, 3))
# fused
q, k, v = ttnn.transformer.split_query_key_value_and_split_heads(qkv, num_heads=h)

# nlp create-qkv-heads (decode; qkv: [b, 1, hidden], seq_len == 1, GQA: h q-heads, kv k/v-heads)
# unfused
q_dim = h * d
kv_dim = kv * d
q = ttnn.slice(qkv, (0, 0, 0), (b, 1, q_dim))
k = ttnn.slice(qkv, (0, 0, q_dim), (b, 1, q_dim + kv_dim))
v = ttnn.slice(qkv, (0, 0, q_dim + kv_dim), (b, 1, q_dim + 2 * kv_dim))
q = ttnn.permute(ttnn.reshape(q, [b, h, 1, d]), (2, 0, 1, 3))    # -> [1, b, h, d] decode layout
k = ttnn.permute(ttnn.reshape(k, [b, kv, 1, d]), (2, 0, 1, 3))   # -> [1, b, kv, d]
v = ttnn.permute(ttnn.reshape(v, [b, kv, 1, d]), (2, 0, 1, 3))
# fused
qkv = ttnn.reshape(qkv, [1, 1, b, hidden])                       # decode op expects [1, 1, batch, hidden]
q, k, v = ttnn.experimental.nlp_create_qkv_heads_decode(qkv, num_heads=h, num_kv_heads=kv)

# nlp concat-heads (decode; x: [s=1, b, h, d])
# unfused
x = ttnn.permute(x, (1, 2, 0, 3))
out = ttnn.reshape(x, [b, h * d])
# fused (x height-sharded in L1)
out = ttnn.experimental.nlp_concat_heads_decode(x_sharded, num_heads=h)

# concatenate heads (prefill; x: [b, h, s, d])
# unfused
x = ttnn.permute(x, (0, 2, 1, 3))
out = ttnn.reshape(x, [b, s, h * d])
# fused
out = ttnn.transformer.concatenate_heads(x)

# rope (prefill, rotate-half; x: [b, h, s, d])
# unfused
half = d // 2
x1 = ttnn.slice(x, (0, 0, 0, 0), (b, h, s, half))
x2 = ttnn.slice(x, (0, 0, 0, half), (b, h, s, d))
rot = ttnn.concat([ttnn.neg(x2), x1], dim=-1)
out = ttnn.add(ttnn.multiply(x, cos), ttnn.multiply(rot, sin))
# fused
out = ttnn.experimental.rotary_embedding(x, cos, sin)

# topk
# unfused
vals, idx = ttnn.sort(x, dim=-1, descending=True)
out = ttnn.slice(vals, (0, 0, 0, 0), (n, c, h, k))         # keep first k on the sort dim
# fused
out, idx = ttnn.topk(x, k, dim=-1, largest=True, sorted=True)
```

RoPE variants: `ttnn.experimental.rotary_embedding` is rotate-half; `rotary_embedding_llama` is the interleaved
form; decode passes `token_index`. One rotary factory applied the compute config to only one core group
([prefill and KV](prefill-kv.md)). TopK width and `k` choose between fast and slow kernels
([perf-advice](perf-advice.md#lm-head-and-sampling)).

## Worked examples: graph rewrites

```python
# repvgg conv-sum
# unfused
y3 = ttnn.conv2d(x, w3, ...)          # 3x3, padding 1
y1 = ttnn.conv2d(x, w1, ...)          # 1x1, padding 0
out = ttnn.add(y3, y1)
# fused (weights combined host-side at load time: w = w3 + w1 padded into the 3x3 center)
out = ttnn.conv2d(x, w3 + pad_to_3x3(w1), ...)   # 3x3, padding 1

# shared-lhs matmul (two or more matmuls on the same input, e.g. QKV or gate/up)
# unfused
o1 = ttnn.matmul(a, b1)
o2 = ttnn.matmul(a, b2)
o3 = ttnn.matmul(a, b3)
# fused (weights concatenated once at load time, not per call)
b = ttnn.from_torch(torch.cat([w1, w2, w3], dim=-1), ...)   # setup
o = ttnn.matmul(a, b)
o1, o2, o3 = <slice o along the last dim by each output width>

# spatial mean (x: [N, H, W, C])
# unfused
out = ttnn.mean(x, dim=[1, 2], keepdim=True)
# fused
x = ttnn.reshape(x, [N, 1, H * W, C])
out = ttnn.mean(x, dim=2, keepdim=True)

# permute-reshape-permute (x: [N, H, W, C])
# unfused
y = ttnn.permute(x, (0, 3, 1, 2))
y = ttnn.reshape(y, [N, 1, C, H * W])
out = ttnn.permute(y, (0, 1, 3, 2))
# fused
out = ttnn.reshape(x, [N, 1, H * W, C])
```

## Worked examples: op merging

```python
# conv + bias
# unfused
y = ttnn.conv2d(x, w)
out = ttnn.add(y, bias)
# fused
out = ttnn.conv2d(x, w, bias_tensor=bias)

# conv + multiply/scale
# unfused
y = ttnn.conv2d(x, w)
out = ttnn.multiply(y, scale)                  # per-output-channel scale
# fused (scale folded into weights host-side at load time)
out = ttnn.conv2d(x, w * scale_per_out_channel)

# conv2d + activation
# unfused
y = ttnn.conv2d(x, w)
out = ttnn.relu(y)
# fused
out = ttnn.conv2d(x, w, conv_config=Conv2dConfig(activation="relu"))

# matmul/linear + activation
# unfused
y = ttnn.matmul(a, b)
out = ttnn.silu(y)
# fused
out = ttnn.matmul(a, b, activation="silu")

# input-arg activation -> binary
# unfused
a_act = ttnn.relu(x)
out = ttnn.add(a_act, y)
# fused
out = ttnn.add(x, y, input_tensor_a_activations=[ttnn.UnaryOpType.RELU])

# matmul + bias -> linear
# unfused
y = ttnn.matmul(a, b)
out = ttnn.add(y, bias)
# fused
out = ttnn.linear(a, b, bias=bias)

# permute/transpose + matmul
# unfused
b_t = ttnn.permute(b, (0, 1, 3, 2))
out = ttnn.matmul(a, b_t)
# fused
out = ttnn.matmul(a, b, transpose_b=True)

# slice after matmul (keep first n_keep columns)
# unfused
y = ttnn.matmul(a, b)
out = ttnn.slice(y, (0, 0, 0, 0), (bsz, 1, m, n_keep))
# fused (slice pushed into the weight at load time -> narrower matmul)
b = ttnn.slice(b, (0, 0, 0, 0), (bsz, 1, k, n_keep))
out = ttnn.matmul(a, b)

# batchnorm (inference) + conv
# unfused
y = ttnn.conv2d(x, w)
inv = ttnn.rsqrt(ttnn.add(var, eps))
y = ttnn.multiply(ttnn.subtract(y, mean), inv)
out = ttnn.add(ttnn.multiply(y, gamma), beta)
# fused (fold the affine into conv weights/bias host-side at load time)
alpha = gamma / sqrt(var + eps)                # host (torch), setup only
out = ttnn.conv2d(x, w * alpha, bias_tensor=(b0 - mean) * alpha + beta)

# pad + pooling
# unfused
x = ttnn.pad(x, pad, value=-inf)               # max_pool pads with -inf; match it
out = ttnn.max_pool2d(x, padding=[0, 0, 0, 0])
# fused
out = ttnn.max_pool2d(x, padding=pad)

# numeric-stable softmax
# unfused
m = ttnn.max(x, dim=-1, keepdim=True)
shifted = ttnn.subtract(x, m)
out = ttnn.softmax(shifted, dim=-1)
# fused
out = ttnn.softmax(x, dim=-1, numeric_stable=True)

# reduction + reshape
# unfused
r = ttnn.sum(x, dim, keepdim=False)
out = ttnn.reshape(r, <input shape with dim set to 1>)
# fused
out = ttnn.sum(x, dim, keepdim=True)

# scaled-sum -> mean  (N = size of the reduced dim)
# unfused
s = ttnn.sum(x, dim, keepdim=True)
out = ttnn.multiply(s, 1.0 / N)
# fused
out = ttnn.mean(x, dim, keepdim=True)

# rope decode / decode-reshape (x: [b, h, 1, d]; decode)
# unfused
rope_out = <rotate-half rope on x>             # same slice/neg/concat + x*cos + rot*sin as prefill
out = ttnn.permute(rope_out, (2, 0, 1, 3))     # -> [1, b, h, d] decode layout
# fused
x = ttnn.permute(x, (2, 0, 1, 3))
out = ttnn.experimental.rotary_embedding(x, cos, sin, token_index=0)
```

Host-side folds (weights, bias, scale, batch norm) run once at load time; the runtime path keeps no torch.
