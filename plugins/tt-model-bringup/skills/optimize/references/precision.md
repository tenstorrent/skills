# Precision, fidelity and compute-kernel configs

Part of [optimize](../SKILL.md): detail moved out of the main skill file. Read only the sections in play.

For decoder or module-level optimization, do not use a blunt global dtype policy. Start with a named precision/fidelity policy and tune tensor groups separately: attention weights, MLP/expert weights, KV cache, activations/residuals, CCL communication, norms, logits, and layer exceptions. Use the fallback policy below as the starting point unless an earlier stage has already selected a faster correct policy. Move one tensor group at a time.

Then tune precision and fidelity one group at a time so regressions can be assigned. For precision tuning always use real weights and recorded input activations; synthetic weights and activations are not representative enough to veto a policy. A test named "representative semantics" is still synthetic if it uses random or synthetic tensors. Synthetic/random-weight tests may catch op crashes, shape bugs, trace replay bugs, and numerical explosions, but they cannot by themselves make a slower higher-precision policy "best correct" when the lower-precision policy passes real-weight evidence for the target model. If synthetic coverage fails while real-weight coverage passes, treat that as a discrepancy to debug: inspect the synthetic distribution and threshold, add real-weight non-aligned/paged/trace replay coverage for the same contract, and reject the lower-precision policy only on model-visible correctness loss, trace/runtime failure, unacceptable latency, or an exact op-contract blocker. A common fallback starting point, when no prior policy exists, is BF16 activations and norms, BFP8 attention/MLP weights, BFP8 KV cache if PCC allows it, and selective BFP4 trials for MLP/expert weights.

If a prior-good policy fails in the generated code, debug the mismatch before discarding it. Check loader grouping, tensor layout, KV-cache update math, scale/transpose handling, and whether the validation harness is exercising the same full-model policy. For KV-cache precision, compare cache shape and mapper as well as dtype. Local-head replicated caches, global-head sharded caches, page-table distribution, and `paged_fill_cache`/`paged_update_cache` input dtype restrictions are different contracts. Lower-precision cache fill should cast the prefill K/V fill tensors to the cache dtype before `paged_fill_cache`; decode update tensors should stay BF16/FLOAT32 for `paged_update_cache`.

When optimizing a complete full model in the repo-local autonomous bringup flow, keep the main focus on full-model parallelism, tracing, sharding, data movement, program configs, compute-kernel configs, and removing host boundaries. `$datatype-sweep` owns the final accuracy/performance frontier, but this pass must still try targeted precision/fidelity changes when the measured full-model decode is materially below a credible target and the decoder-layer roofline says reduced precision could be the difference. Do not reject such work as "datatype sweep" by default. Try small, evidence-backed policies such as MLP gate/up BFP4, selected layer exceptions, KV/cache/CCL dtype changes, or compute-fidelity changes, then validate on the same traced full-model token-out path. Leave broad Pareto exploration to `$datatype-sweep`.

## Precision And Fidelity

- Start optimized decoder with BF16 activations and BFP8 weights. Keep norms BF16.
- Try BFP8 KV cache. Keep it if PCC remains above threshold and perf/memory improve. If BFP8 KV fails, inspect the prefill fill-cache dtype path before concluding the dtype is invalid: cache-fill tensors should be explicitly typecast to the cache dtype, while decode `paged_update_cache` inputs should remain BF16/FLOAT32.
- Cache-dtype evidence must include a short-prefix case (about 32 cached tokens, then decode) and a per-user batch-32 control against the same inputs, not only a long-prompt layer PCC. A BFP8 cache that passes a 1024-token layer check can still fail the 0.995 per-user bar on short prefixes, where few cached rows carry the attention (Qwen3-8B: 0.9942 with BFP8 versus 0.9993 with BF16 on the same inputs).
- Try BFP4 for MLP FF1/FF3; these often tolerate BFP4 well.
- Try BFP4 for FF2/down-projection, but expect it to be more sensitive. Fall back based on PCC evidence, not preference.
- If dense MLP or expert matmuls dominate decode time, measure a BFP4/LoFi MLP candidate before accepting the decoder: a BF16-activation/BFP8-weight baseline, a BFP4 FF1/FF3 or gate/up candidate, and a guarded FF2/down BFP4 candidate when the model can tolerate it. Record the results as candidates for `$datatype-sweep`; do not ship BFP4 as the default from this stage. The default stays BFP8, and stage 8 makes the final choice with the P1 reasoning-text precision probe (`precision_reasoning`). Prioritize the measurement against prefill work using the measured latency target.
- For BFP8 weights, HiFi2 is a safe starting point, not a default answer. Try LoFi for dominant decode projection groups and keep it when full-model accuracy and qualitative generation still pass. Record BFP8+LoFi versus BFP8+HiFi2 as separate candidates, because the speed difference can be large even when dtype is unchanged.
- For BFP4 weights, LoFi is expected.
- For BF16 weights or numerically sensitive operations, use HiFi4 or FP32 accumulation where PCC demands it.
- Evaluate precision changes one group at a time so regressions can be assigned to the right tensor group.
- A small or moderate PCC drop after lowering precision may be real precision loss and can reject that dtype with evidence. A catastrophic PCC collapse, such as PCC near zero, output magnitude explosion, NaNs, or a drop from passing to about half-scale or worse, is more likely an implementation, dtype propagation, layout, cache, scale, or validation-harness bug than proof that the lower precision is invalid. Immediately investigate that failure instead of recording "BFP8/BFP4 invalid." First reproduce it with a minimal same-contract test and compare intermediate tensors to the higher-precision path. Then use `$autodebug` with the failing command, logs, tensor shapes, dtype policy, and first bad tensor; use `$autofix` when the suspected bug is concrete. Reject the dtype only after the debug loop shows model-visible precision loss, an exact op-contract blocker, or an unfixed bug with preserved evidence.
- Activation size matters for CCLs. Try using BFP8 activations and see if PCC (and final top-1/top-5/benchmark eval scores if run) remain high enough. Do not treat one global BFP8-activation switch as a complete activation precision sweep. Attention activations, CCL payloads, MLP/FFN activations, MoE expert activations, residual/norm tensors, and logits can have different precision needs and different performance impact. Test BFP8 activations separately for the attention path and for the MLP/FFN or MoE path under the same topology before using a global BFP8 activation policy. If global BFP8 fails, isolate which path fails instead of falling back to BF16 everywhere. If global BFP8 passes but one path is slower or less accurate, keep a mixed BF16/BFP8 policy with evidence.
- Test precision and topology together when tensors cross devices. Activation dtype changes mainly affect CCL and layout traffic, so a BFP8-activation or BFP4-weight candidate tried under one residual/collective topology does not reject it for another topology. For dominant decode paths, keep a small combined matrix crossing activation/CCL dtype, material weight dtype, compute fidelity, and topology family. When any BFP8 activation, BFP4 weight, or LoFi candidate is rejected, state the residual/collective topology it used and whether the same dtype/fidelity was tested with the best topology candidate.
- Prefer fused CCL + matmuls where possible. If a collective output feeds a matmul, try to make the tensors satisfy fused all-gather-matmul or equivalent contracts. If a matmul output is immediately reduced, try the fused matmul-plus-reduce path where one exists. Adapt shape, rank, padding, sharding, and weight layout before rejecting the fused path; a first API validation failure is evidence about the current tensor contract, not proof that the optimization is impossible.
- Otherwise use async CCLs but be careful to ensure there are sufficient semaphores - other models have some good examples of CCL helper classes that track these.
- Always test with watcher when using async CCLs, it's easy to make mistakes that end up in data corruption or hangs.

## Compute Kernel Configs

Common Wormhole-style starting points:

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

Use the architecture-appropriate config class when optimizing non-Wormhole targets.

For larger-core devices, do not hard-code a Wormhole-shaped 8x4 or 32-core decode layout as the only optimized layout. Sweep legal core grids as a coherent layout: residual/intermediate memory configs, norm program grids, matmul program configs, and shard specs must agree. A first validation error such as a shard grid not fitting an op's program grid means adapt the layout/program-grid contract or use `$autofix`; it is not enough evidence to reject the larger grid.
