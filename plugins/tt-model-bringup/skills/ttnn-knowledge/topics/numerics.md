# Numerics

**PCC cannot see a uniform gain error.** Several real biases passed PCC: BF16 accumulation biasing large-K
matmuls, LoFi truncating operands, the FPU truncating FP32 operands to fewer mantissa bits, BFP4 packing taking
the block exponent before rounding (a clamp), and a small gain inside a fused recurrence op. A least-squares gain
check against the reference, beside PCC, found each of them.

**Small differences flip discrete choices.** A difference far below what PCC shows can change which experts a
router picks or which token top-k keeps, many layers later. Comparing generated tokens (batched against alone,
TT against the reference) caught what tensor comparisons missed.

**Batch- or slot-dependent results.** Seen causes: a rotary-embedding factory that applied the requested compute
config to one core group but not the other (precision depended on which slot a user landed in); a grouped MoE op
whose accumulation order depended on the other rows in the batch. Both looked like nondeterminism.

**Long-context attention statistics.** TTNN SDPA kept its running softmax statistics in BF16 even with FP32
accumulation. At long context the number of chunks each worker processes sets the error: decode at very long
context failed with the default config, and batched long-context decode drifted after a few hundred steps. A
larger chunk size (fewer sequential updates per worker) fixed both.

**Recurrent and linear-attention state.** Recurrent state kept in FP32 can still lose precision inside a kernel
that unpacks through TF32 or uses the FPU for decay and subtraction; at long sequence the state drifted. A
kernel that moves the state through the destination registers and uses the SFPU for those steps held FP32.
Algebraically equal formulations with different rounding order diverged after many decode steps; using the same
operation as the reference path did not.

**Fused paths change rounding.** A fused prefill projection with one large K block rounded differently from the
separate path and produced long free-running repetition while every short check passed. Fused variants are worth
a bitwise comparison with the plain path before the accuracy gate.

**Padding can produce NaN.** An all-padding attention group computed 0 × 1/0; multiplying by a mask passes NaN
through (0 × NaN = NaN). Padding rows then leaked into real rows through masked matmuls. Zeroing padding rows and
selecting instead of multiplying fixed it.

**Loops in long generations** came, in one model, from BFP4 weights at the loop's entry: BF16 or BFP8 weights
matched the reference. Telling quantization from a kernel bug took a full-precision run against the reference
(see `reference-accuracy`).

**Ties from softcapping.** A BF16 softcap on logits creates many exact maxima; a sampler that keeps a fixed
number of candidates per shard and breaks ties by id then gives run-dependent picks when the logits themselves
move slightly.

## Choosing a precision policy

- **The readiness reference is lenient.** It is an HF-greedy continuation, so most positions have large margins and
  a precision loss rarely flips them. In one bringup a BFP4 MLP policy scored 0.92 to 0.97 top-1 on it and 0.82 to
  0.85 over 1468 natural-text positions (real next tokens of the readiness book, scored per position against HF);
  the readiness reference alone would have selected it.
- **Top-1 has a binomial margin.** Over n tokens at rate p the standard error is sqrt(p(1-p)/n): about 0.022 at
  p = 0.95 and n = 100, a 95% interval of about +/-0.04. A one-token difference between candidates is noise; more
  tokens or the natural-text reference separate them.
- **Run-to-run spread** sets what counts as a speed gain: measure the baseline at least twice, serially, before
  ranking candidates.
- **Resumed prefill after a precision change.** The resumed and fresh TT paths can agree with each other while
  both drift from HF at hard positions, so a TT-versus-TT resume check fails with a correct cache. A per-position
  comparison against HF (margin probe) separates this from a cache or position bug
  ([prefill features](../../vllm-integration/references/prefill-features.md#generator-level-resume-check)).
- **Restore order.** The sweep's default order for backing out BFP4 (FF2/down, then QKV, WO, FF1+FF3/gate+up)
  puts the groups usually most sensitive first; it is a heuristic, not a measured ranking for any one model. Router
  and routing weights are more sensitive than expert matmuls ([moe](moe.md#precision)).

## When an accuracy failure follows a precision change

A failure that appears after a dtype, cache dtype, math fidelity or CCL payload change marks an error-budget
boundary, not yet a root cause. Write down the whole active policy first (activation dtype, every weight group,
fidelity per projection, cache dtype, CCL payload dtype, page and block size, cache layout, page-table policy,
decode update op), then run controls that separate a real cache or kernel defect from a policy margin:

- the same cache dtype with higher-precision weights, math and CCL payloads;
- the same weights and math with only the cache dtype changed;
- an exact page, block and cache-shape probe that imports the model's constants rather than hard-coding page
  sizes, local KV heads, batch slots or mapper behaviour;
- a mature implementation that uses the same low-precision cache path on a comparable repeated-decode workload,
  when one exists.

"BF16 fixes it" does not by itself show that a BFP8 cache is unstable, that `paged_update_cache` is defective, or
that accumulated error is the cause; keep those as hypotheses until the controls isolate them. A reduced probe
whose page size, batch width, local KV heads, update row shape, mesh mapper or page-table policy differ from the
full model is smoke evidence, not causal proof.
