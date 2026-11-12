# Optimization method and scope

Part of [optimize](../SKILL.md): detail moved out of the main skill file. Read only the sections in play.

This guide does not choose a completely new model-level parallelization strategy from scratch, such as TP vs DP vs EP for the whole model. But if you are optimizing an existing multi-device TTNN path, changing activation layout, residual layout, collective placement, fused CCL+matmul use, and helper-module decomposition is in scope. Do not treat the inherited multichip implementation as fixed when the perf report shows material collectives, resharding, or layout movement.

Read the advice in `tech_reports/LLMs/llms.md`, particularly section 4 "Best practices and optimizations". In this skill we will strive to optimize *on-device* performance. For decode it is required to always measure the performance of a traced execution run; untraced/eager decode performance is not acceptable optimized evidence. Teacher-forcing decode must also use the traced path. For complete model or serving paths, avoidable host gaps are part of the optimization target and must be removed rather than merely noted. Always perform optimization using real tensor shapes, sequence shapes, batch size, sharding, and dtypes. Do not shrink hidden sizes, head counts, sequence lengths, or weight shapes just to make evidence easier to collect.



Optimization must preserve the model's capability and context contract. If a change affects KV-cache dtype, cache layout, trace buffers, activation memory, CCL buffers, or any other persistent allocation, update `models/autoports/<model>/doc/context_contract.json`. Do not improve performance by lowering `max_model_len`, benchmark context, eval context, or any other advertised capability. A reduction is acceptable only when a hard physical device limit prevents the advertised capability from fitting or running, such as device DRAM capacity for weights + KV/cache/state + required persistent buffers. If reduced, record the byte calculation or failed capacity probe, the largest feasible supported value, and the exact construction/serving setting that uses it.

Optimization must also preserve valid non-aligned logical sequence lengths. Faster code that only works when prompt or prefill length is divisible by a chunk, tile, block, page, or trace size is not complete; keep padding, masking, cache fill, position handling, and output slicing inside the model path.

When direct traced generator decode is already fast but vLLM/serving decode is slower, treat the gap as orchestration overhead before retuning decoder math. First fix the adapter/generator path: async decode split, nonblocking trace replay, on-device traced sampling, host readbacks, page-table/input refreshes, and fallback sampling. Keep same-harness primary single-user and CI serving-burst before/after metrics.

A note on the term "sharding" - tt-metal uses this to mean two things. On-device sharding means sharding across the cores or DRAM banks of one device, such as L1-sharded activations or DRAM-sharded weights. Multi-chip sharding means distributing tensors across devices in a mesh. On-device sharding is in scope for this skill. When `tt-perf-report` mentions sharding, it usually means on-device sharding.

Profile warmed prefill and decode separately. Use `tt-perf-report` to find bottlenecks and suggestions for decoder, module, and non-serving full-model optimization. Try applicable advice. Keep changes that improve the target without unacceptable correctness or complexity cost. Record why rejected advice was rejected. If advice seems wrong, incomplete, or misleading, call that out as a candidate improvement to `tt-perf-report`.

Before local knob tuning, do an operation-topology audit of the measured path. Read the code and perf report together, then write a small table with the current operation sequence, material repeated matmuls, material collectives, reshard/layout conversions, candidate fused or lower-movement replacements, dtype/fidelity constraints for each candidate, and the action taken. This is not a comparison to any one existing model. Derive it from the target model's dataflow: if multiple projections consume the same activation, if a collective feeds a matmul, if a matmul is immediately followed by a collective, or if tensors gather/reshard only to satisfy a local helper, that is optimization work.

For multi-device decode, audit topology as coherent families, not isolated knobs. For every material row/column-parallel boundary, compare the feasible families:

- local matmul followed by all-reduce or all-gather to a replicated output;
- matmul followed by reduce-scatter, with a later hidden all-gather only if the next op or residual contract needs it;
- all-gather followed by matmul fused into one op when a matmul consumes gathered input;
- matmul output fused with reduce-scatter or all-reduce when supported;
- residual kept sharded or fractured with distributed norm/residual ops instead of gathered to a replicated boundary.

For each candidate, record residual layout before and after, collective axis and dtype, expected bytes moved, ops removed or added, persistent-buffer use, and next-layer compatibility. If a fused or lower-movement candidate is slower, prove it was tried under a compatible residual/layout contract. Otherwise redesign the contract and remeasure before rejecting the family.

Do not measure a lower-movement collective family only after restoring the old replicated residual layout. That is a useful compatibility candidate, but it does not reject the family whose purpose is to carry a sharded or fractured residual forward. If a reduce-scatter, fused matmul+reduce-scatter, or fused all-gather-matmul candidate should win by changing the residual contract, adapt the next residual, norm, attention, or MLP boundary to consume that layout and measure the stack-compatible path. For single-layer correctness comparison, gather or convert only in the test harness outside the measured layer path, and label that boundary cost separately. Reject the lower-movement family only after this adapted path is measured slower or a minimal repro proves the required next op cannot consume the layout.

Optimize against the best correct measured path you have, not only against the original functional path. If this checkout or run already contains an earlier optimized artifact, read its compact perf summary and `tt-perf-report` tables before accepting a new candidate. If there is no earlier optimized artifact, keep a candidate table during this stage. A candidate is not accepted just because it has fewer ops or a nicer topology; it must beat the best correct candidate for the target workload, prefill/TTFT and traced warmed decode as applicable. If a topology change forces a worse dtype, fidelity, layout, or program config, compare that full candidate against the dtype-compatible separate path and keep the faster correct path. Do not reject a dtype, fidelity, fusion, or CCL candidate only because it loses in isolation; if the candidate changes communication volume or residual layout, measure it as part of the compatible topology family.

Before finalizing, rerun the selected default path with the same evidence harness used for candidates. The headline optimized numbers are the final default-run numbers, not the best candidate run copied from an earlier environment. If the final default run is materially slower than the candidate it is meant to preserve, fix the default wiring or explain the difference before completing the stage.

When this skill is invoked as part of vLLM integration or optimized-vLLM, do not collect Tracy, `tt-perf-report`, or `TT_METAL_DEVICE_PROFILER` metrics from the live vLLM server or serving adapter. The profiler/table requirement does not apply to vLLM serving stages. Use same-harness `run_vllm_server` benchmark JSON for before/after serving performance: primary single-user 128/128/1 for headline decode t/s/u and CI serving-burst 100/100/32 for vLLM-nightly parity and serving-capacity context. Also keep sampling, qualitative, degenerate-output, async-split, stale-input, and no-host-fallback evidence. Use earlier full-model or reduced non-serving profiles for device-op context if available; if not, record that evidence gap instead of profiling the vLLM stage.

Every optimization stage that can generate text must preserve prompt-correct qualitative behavior. From optimized-full-model onward, use `$qualitative-check` to rerun the shared qualitative prompt suite after selecting the optimized path. If text quality changes after an optimization, compare against a prompt-correct HF or previous-stage control before blaming the checkpoint.

Do not run Tracy or device-profiler collection on a full-model stack with every layer present. Full-stack profiling can create multi-GB profiler dumps, overflow device-profiler buffers, and distort the measurement. For full-model profiling, build a reduced profiling variant with one real layer of each layer kind and the real surrounding path: embeddings or input projection, the representative layers, final norm, LM head, sampling or token feedback when relevant, real KV-cache/page-table shapes, and the same trace path. Capture one warmed traced decode replay, or the smallest signposted prefill/decode window that answers the question. Use this reduced-layer profile for `tt-perf-report`; use the complete model only for end-to-end timing and correctness.


Before finishing non-vLLM optimization, review a current `tt-perf-report` output. If an applicable optimization remains untried, try it. If it fails, debug the failure. If a TTNN op or runtime limitation blocks the optimization, keep a small repro or exact failure evidence. Do not leave a known optimization for a later stage unless another skill explicitly owns it. For vLLM serving optimization, review the serving benchmark and contract evidence instead; do not create a profiler run to satisfy this paragraph.

Sometimes you will encounter a ttnn limitation or a bug. If, for example, you try an optimization and find that L1 buffers overlap (insufficient L1 space) do not take this as an excuse to give up on that optimization entirely. Instead, dive in to the code of the op and its shapes and configs and understand how you can reduce the L1 requirements in this part of the model. Or perhaps your specific shapes is not supported by the op and you need another one. Or the op does not support padding -> change the model contract so the tensors are manually padded in torch before conversion - all these things are in scope. If the failure crosses several ops, kernels, layouts, or planner/runtime boundaries and you are not making progress, use `$autofix`; it will run `$autodebug` if needed, then verify or refute each proposed bug before keeping any fix. Solve problems. Be curious. Be tenacious. Be creative. Be brilliant!

# Useful Optimization Knowledge

Use this reference while optimizing functional TTNN code. It captures repo-local optimization patterns and the strongest current LLM guidance. If you are not optimizing an LLM, use your best judgement about what applies in your case.

## Code Paths Worth Reading

- `tech_reports/LLMs/llms.md`: LLM memory configs, matmul variants, DRAM-sharded matmul guidance, and perf-report interpretation.
- tt-transformers `src/tt_transformers/modules/attention/attention_1d.py`: reusable attention configs with BFP8 attention weights, BFP8 KV cache, DRAM-sharded decode matmuls, SDPA configs, and L1-sharded decode residual paths.
- tt-transformers `src/tt_transformers/modules/mlp/mlp_1d.py`: decode/prefill MLP split, DRAM-sharded decode matmuls, sharded outputs, and precision knobs.
- tt-transformers `src/tt_transformers/modules/lm_head/lm_head_1d.py`: reusable LM-head output projection with vocab splitting, LM-head dtype, DRAM-sharded weight memory config, input/output memory configs, and decode program config.
- tt-transformers `tests/modules/lm_head/test_lm_head_1d.py`: expected LM-head construction, weight splitting, memory config, and PCC checks.
- `models/common/tensor_utils.py`: helpers to serialize program and compute-kernel configs for artifact reporting.
- `ttnn/cpp/ttnn/operations/matmul/device/factory/matmul_multicore_reuse_mcast_dram_sharded_program_factory.cpp`, `ttnn/cpp/ttnn/operations/matmul/device/utilities/matmul_utilities.cpp`, and `tests/ttnn/nightly/unit_tests/operations/matmul/test_matmul_dram_sharded.py`: Blackhole multi-reader DRAM-sharded matmul implementation, legality checks, and focused coverage.
- `models/common/sampling/generator.py` and tt-transformers `src/tt_transformers/modules/sampling/sampling_1d.py`: common on-device sampling implementations to compare before optimizing token-out sampling.
- `models/demos/gpt_oss/tt/experts/README.md`, `models/demos/gpt_oss/tt/experts/decode.py`, `models/demos/gpt_oss/tt/experts/prefill.py`, `models/demos/gpt_oss/tt/experts/weights.py`, `models/demos/gpt_oss/tt/experts/config.py`, and `models/demos/gpt_oss/tt/topk.py`: default routed MoE active-expert path using `ttnn.sparse_matmul`.
- `models/demos/gpt_oss/tt/`, `models/demos/gemma4/tt/`, and `models/demos/deepseek_v3/tt/`: model-specific examples where common modules do not fully fit.

## Core Optimization Rules

- Your initial functional test suite remains the correctness floor. Rerun the same functional prefill, decode, PCC, paged KV-cache, determinism, stress, trace, and watcher checks against the optimized path before accepting performance wins.
- Avoid data movement before tuning math. A slightly smaller core grid can beat a faster individual op if it avoids resharding between ops.
- Optimize the operation topology before fine-tuning individual op configs. Count material matmuls, collectives, reshard/layout conversions, and host/device crossings in the measured path. A path with extra high-cost ops is not optimized just because each individual op has a reasonable program config.
- Decode activations should generally stay width-sharded in L1 across norm, attention, residual, MLP, and output projection boundaries.
- Prefill activations are usually large and often belong in DRAM interleaved; use 2D matmul program configs for large prefill matmuls.
- Use SDPA/FlashDecode/FlashAttention ops instead of hand-built attention primitives when the target model fits their contracts.
- Explicitly configure `memory_config`, `program_config`, and `compute_kernel_config` for important ops. Defaults are often correct but suboptimal.
- Choose shard specs and core grids that divide tensor dimensions cleanly into tiles. Padding in sharded paths is a common source of bugs or wasted work.
- For DRAM-sharded decode matmul, weights should be width-sharded in DRAM and activations/outputs width-sharded in L1 on the matching core grid.
- Optimize for the eval workload: the largest served batch with long answers; batch 1 stays a regression row and the FUNCTIONAL bar. This is not permission to drop batch-1 performance or larger-batch support; preserve correct batch handling and verify up to batch/concurrency 32 for complete model and serving paths when hardware and memory allow it. For MoE decoders on non-Galaxy systems, preserve gate-selected active-expert execution and prefer the GPT-OSS `ttnn.sparse_matmul` path for sparse expert projections plus score weighting and expert reduction. Dense all-expert execution is a debug baseline, not the optimized target.
