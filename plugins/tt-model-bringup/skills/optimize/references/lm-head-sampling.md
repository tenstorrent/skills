# Full-model decode closure, LM head and sampling

Part of [optimize](../SKILL.md): detail moved out of the main skill file. Read only the sections in play.

## Full-Model Decode Closure

For optimized full-model work, first compute a target budget from the best decoder-layer evidence:

- `layer_stack_ms = sum(layer_count[kind] * optimized_multichip_decode_ms[kind])`;
- `layer_stack_tps = 1000 / layer_stack_ms` for batch-1 single-user decode;
- `full_model_overhead_ms = measured_full_model_ms_per_token - layer_stack_ms`.

If the layer-stack estimate is already slower than the target, return to decoder optimization before spending time on generator orchestration. If the layer-stack estimate can meet the target but the full model cannot, optimize the overhead explicitly before changing the mathematical core: final norm, LM head, logits movement, sampling trace, token/current-position/RoPE/page-table refresh, trace replay blocking, synchronizations, host readbacks, cache management, and CCL buffer lifetime. For token/current-position/RoPE/page-table refresh specifically, the optimized steady-state loop should use persistent device tensors, `tt_out_tok` feedback, device-side position advance for fixed-step decode, and page-table copies only when the page table changes.

### LM Head And Sampling

For models with an LM head and token sampling, treat the terminal path as part of optimized decode. A fast decoder layer stack is not enough if final norm, LM head, logits movement, sampling, or token feedback add avoidable per-token work.

Before accepting full-model token-out decode performance:

- Profile a reduced full-model token-out trace that includes final norm, LM head, logits movement, sampling, and token feedback. Measure these terms separately from the decoder-layer stack.
- Treat the LM head as a real decode matmul, not as small postprocessing. It is a hidden-size by vocab-size projection and is usually DRAM-bound.
- Keep the hidden stream in the optimized sharded layout through final norm and into the LM head when possible. Do not gather a full hidden vector or full logits tensor merely because it simplifies the wrapper.
- Split the LM head over the mesh and vocab dimension when running on more than one device. Each device should compute a shard of the vocabulary rather than every device computing replicated full-vocab logits.
- Use `LMHead1D` from tt-transformers `src/tt_transformers/modules/lm_head/lm_head_1d.py` if you can. It is already well optimized for 1D mesh LM heads. If, after debugging shapes, sharding, and weight loading, you cannot make it support the target model, use it as the template for how to optimize your implementation.
- Use the intended LM-head weight dtype, DRAM-sharded or ring matmul program configs, and output memory config for decode. Avoid a single replicated full-vocab BF16 matmul as the default terminal implementation.
- Pad LM-head weights when needed to make the vocab-sharded decode matmul legal and fast, including DRAM-sharded matmul shapes. Keep the real tokenizer vocab size as separate metadata for sampling; the padded LM-head width is the tensor shape, not the valid token range.
- Put logits into the layout the sampler expects. Design the LM-head and sampling boundary so full-vocab all-gather is not in the hot path.
- Mask padded vocab IDs inside each local logits shard before force-argmax or local TopK. Use very negative values for token IDs `>= vocab_size`. Zero-padding LM-head weights is not a sampling mask, because those padded columns produce zero logits that can beat negative real logits.
- Make the sampler's local TopK input width friendly to the fast TopK path. On current TTNN paths, a non-power-of-two local vocab shard can fall back to a slow single-core `TopKDeviceOperation`. After invalid vocab IDs are masked, pad each local logits shard to a power-of-two width before TopK when needed, using very negative values and invalid indices for the extra power-of-two tail. Do not accept a multi-ms, one-core TopK as a final decode path. Read the routing rule in the TopK op source for the target architecture before padding: on Blackhole a `k` above 64 routes to the large-indices composite regardless of width, and on one model that full-row composite measured faster than every power-of-two chunking. Measure both paths; padding is a candidate, not a default.
- Use a common sampling implementation for token-out decode. Compare `models/common/sampling/` and tt-transformers `src/tt_transformers/modules/sampling/sampling_1d.py`, choose the one that fits this model's state, seed, topology, trace, and logprob requirements, and record the choice. Trace the chosen sampling path or a correct generator-owned wrapper around it. Pass `tt_out_tok=<persistent decode token input tensor>` so the sampled token becomes the next token input on device.
- Keep sampling trace keys distinct for greedy, penalties, and log-prob modes. Warm and capture the active mode before measuring.
- For vocab-sharded greedy decode, keep the split-sampling tensors tile-shaped. A good default is local `topk(..., k=max_top_k)`, usually `max_top_k=32`, on each vocab shard; all-gather those candidates; then pass sampling params that are semantically greedy (`k=1`, `p=0`, `temp=1`). Do not build a physical `top_k=1` per-shard path if it creates a gathered width smaller than a tile or forces a fallback. This keeps greedy behavior while avoiding full-vocab all-gather plus global argmax.
- For greedy decode, use the vocab-sharded split-sampling path above. Do not replace it with full-vocab all-gather plus global argmax because an unpadded split path is slow; fix the split path first.
- The split-sampling greedy benchmark must be semantically greedy. Do not use a generic sampled `top_k=32` or top-p-capable path as the only comparison against force-argmax. If `top_k=1` or equivalent greedy split sampling fails because of sampler shape, layout, or tiling requirements, fix that contract or keep a minimal repro and leave the stage incomplete.
- If `ArgMaxDeviceOperation`, full-vocab all-gather, generic `TopKDeviceOperation`, or sampling trace replay dominates token-out decode, fix the LM-head/sampling contract before retuning decoder dtypes or CCLs. Do not mark the optimization complete with this bottleneck still in the measured path.
- Make vLLM reuse the same optimized terminal path. Do not add adapter-side host argmax, full-logits readback, or a separate fallback sampler for serving.

For vLLM serving performance, do not profile the live server or serving adapter to split these terminal costs. Reuse the full-model or reduced non-serving terminal evidence above, then prove the serving adapter uses that path with same-harness benchmark JSON and contract checks.

The same measured path must be used for before/after comparisons. A teacher-forcing or device-logit replay number is useful, but it does not prove a token-out generator or vLLM path is fast unless it includes the same sampling and token-feedback work. Record both when they differ.

If a decoder optimization was disabled in the full model because the stacked model hit L1, semaphore, trace, or CCL limits, do not accept the fallback as final until you have tried to reduce or pool that resource. Examples include persistent CCL buffers, output buffers, ring buffers, semaphores, trace input tensors, and page-table buffers. If it still cannot fit, record the exact allocation or runtime failure and the measured cost of the fallback.

Preserve the multichip decoder's data-layout contract across the stack. If the decoder was optimized around a sharded/fractured residual stream, do not insert a layer-to-layer all-gather merely to simplify the full-model wrapper. Try fused collective/matmul or sharded-output patterns first and find a way to make the performant solution work. $autofix can help you if you are running into bugs here.
