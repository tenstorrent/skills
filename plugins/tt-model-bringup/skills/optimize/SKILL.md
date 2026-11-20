---
name: optimize
description: Optimize per-device performance of runnable TTNN code, preserving correctness while improving layout, precision, sharding, program configs, data movement, and warmed latency with tt-perf-report evidence except in vLLM serving stages, where profiler collection is intentionally skipped.
---

# Optimize TTNN code

Before using this workflow, follow the dependency and environment setup in
[the model-bringup entrypoint](../model-bringup/SKILL.md#startup).
Resolve its package root from this installed skill's location; do not assume
this plugin is copied into the target tt-metal checkout.

## Scope

This skill starts from runnable TTNN code with passing correctness tests; if they do not pass, use the bringup or
debugging skill first. It is written for autoregressive LLMs with prefill and decode. For another kind of model,
map each requirement to the nearest equivalent path and record the mapping; do not drop correctness or performance
evidence.

- In scope: on-device sharding (over the cores or DRAM banks of one device, which is what `tt-perf-report` usually
  means by sharding), layouts, precision, program configs, data movement, host gaps. On an existing multi-device
  path also activation and residual layout, collective placement, fused CCL+matmul and helper decomposition: the
  inherited multichip implementation is not fixed when the report shows material collectives, resharding or
  layout movement.
- Out of scope: choosing a new whole-model parallelization strategy (TP vs DP vs EP) from scratch.

Read section 4 of `tech_reports/LLMs/llms.md`. Read [performance advice](../ttnn-knowledge/topics/perf-advice.md)
and [matmul and precision](../ttnn-knowledge/topics/matmul-precision.md) before tuning; the other knowledge topics
are linked where they apply.

## Ground Rules

- The functional test suite is the correctness floor: rerun its prefill, decode, PCC, paged KV-cache,
  determinism, stress, trace and watcher checks on the optimized path before accepting a performance win.
- Decode performance is measured on a traced, warmed run; eager decode is not optimized evidence. Teacher-forcing
  decode also uses the traced path.
- Optimize at real tensor shapes, sequence lengths, batch sizes, sharding and dtypes. Do not shrink hidden sizes,
  head counts, sequence lengths or weights to make evidence easier.
- Optimize for batch 1 and the largest batch in `batch_sizes` (default 1, 8, 16, 32), the target workload in
  [`agentic-serving`](../agentic-serving/SKILL.md); the other batches must still work. Spend effort on batch 1
  first, then the largest batch, then the rest. Hard-code batch 1 nowhere (trace inputs, cache and page tables,
  sampling, output). For complete-model and serving work, keep evidence that batches up to the largest in
  `batch_sizes` still work where hardware, memory and harness allow.
- Prefill latency and time to first token (TTFT) are targets alongside decode. Prioritize the measured bottleneck
  of the requested workload.
- Preserve the capability and context contract. A change to KV-cache dtype or layout, trace buffers, activation
  memory, CCL buffers or any other persistent allocation updates `models/autoports/<model>/doc/context_contract.json`.
  Do not gain speed by lowering `max_model_len`, benchmark context, eval context or any advertised capability. A
  reduction is acceptable only for a hard physical device limit (for example DRAM for weights + KV/state +
  required persistent buffers); record the byte calculation or failed probe, the largest feasible value and the
  construction or serving setting that uses it.
- Preserve valid non-aligned logical sequence lengths: padding, masking, cache fill, positions and output slicing
  stay inside the model path. Code that only works for lengths divisible by a chunk, tile, block, page or trace
  size is not complete.
- For complete-model and serving paths, avoidable host gaps are part of the target and are removed, not noted.
- Optimize against the best correct measured path, not only the functional one. Read the compact perf summary and
  `tt-perf-report` tables of an earlier optimized artifact when one exists; otherwise keep a candidate table in
  this stage. A candidate is accepted only when it beats the best correct candidate on the target workload
  (prefill/TTFT and traced warmed decode as applicable), not because it has fewer ops. If a topology change forces
  a worse dtype, fidelity, layout or program config, compare the full candidate with the dtype-compatible separate
  path. A dtype, fidelity, fusion or CCL candidate that changes communication volume or residual layout is measured
  inside its compatible topology family, not only in isolation.
- Before finishing, rerun the selected default path with the candidate evidence harness. Headline numbers come
  from this final default run. If it is materially slower than the candidate it should preserve, fix the wiring or
  explain the difference.
- Every stage that generates text keeps prompt-correct qualitative behaviour. From optimized-full-model on, rerun
  the shared prompt suite with `$qualitative-check` after selecting the path; if text quality changes, compare with
  a prompt-correct HF or previous-stage control before blaming the checkpoint.
- When direct traced generator decode is fast but vLLM decode is slower, fix orchestration before decoder math:
  async decode split, nonblocking trace replay, on-device traced sampling, host readbacks, page-table and input
  refreshes, fallback sampling. Keep same-harness primary single-user and CI serving-burst before/after metrics.

### Profiling, watcher and devices

- Profile warmed prefill and decode separately and use `tt-perf-report` for decoder, module and non-serving
  full-model work. Commands, signposts, time units, duration sanity checks before roofline math, known tooling
  failures and which raw dumps stay out of copied-back artifacts: [profiling](../ttnn-knowledge/topics/profiling.md).
- Never run Tracy or the device profiler on a full-model stack with every layer. Profile a reduced variant with one
  real layer of each kind and the real surrounding path; use the complete model only for end-to-end timing and
  correctness.
- vLLM integration and optimized-vLLM stages collect no Tracy, `tt-perf-report` or `TT_METAL_DEVICE_PROFILER`
  data from the live server or serving adapter; the profiler requirements of this skill do not apply there. Use
  same-harness `run_vllm_server` benchmark JSON: primary single-user 128/128/1 for headline decode t/s/u and CI
  serving-burst 100/100/32 for nightly parity and capacity. Also keep sampling, qualitative, degenerate-output,
  async-split, stale-input and no-host-fallback evidence. Use earlier non-serving profiles for device-op context,
  or record that the evidence is missing.
- Watcher and profiler evidence are separate hardware runs.
- Device commands, resets, hangs and ARC/ERISC/remote-Ethernet recovery follow `$tt-device-usage`, including its
  [profiler section](../tt-device-usage/SKILL.md#profiler-runs-that-wedged-a-t3k). A board that becomes
  undiscoverable after watcher, profiler or serving trouble is an infrastructure event, not a model blocker, until
  that recovery fails. Resume the same stage after recovery (`--resume-stage <stage_number>` in the multigoal
  flow), check that the resumed objective names the same model and stage skill, and record the recovery in the
  work log as infrastructure evidence.

## Operation-Topology Audit

Before local knob tuning, read the code and the perf report together and write a table: current operation
sequence, material repeated matmuls, material collectives, reshard and layout conversions, candidate fused or
lower-movement replacements, dtype and fidelity constraints per candidate, action taken. Derive it from the
model's own dataflow: several projections on one activation, a collective feeding a matmul, a matmul followed by a
collective, or a gather or reshard that only satisfies a local helper are all optimization work.

For multi-device decode, compare every material row/column-parallel boundary as coherent families:

- local matmul followed by all-reduce or all-gather to a replicated output;
- matmul followed by reduce-scatter, with a later all-gather only if the next op or residual contract needs it;
- all-gather fused with the matmul that consumes the gathered input;
- matmul output fused with reduce-scatter or all-reduce where supported;
- residual kept sharded or fractured with distributed norm and residual ops.

For each candidate record residual layout before and after, collective axis and dtype, expected bytes moved, ops
removed or added, persistent-buffer use and next-layer compatibility. A lower-movement family is measured with the
next residual, norm, attention or MLP boundary adapted to consume its layout, not after an immediate restore of the
old replicated layout (that restore is a separate compatibility candidate). For single-layer correctness, gather
only in the test harness, outside the measured path, and label that cost. Reject the family only when the adapted
path measures slower or a minimal repro shows the next op cannot consume the layout.

## Precision Policy

Do not use one global dtype for decoder or module work. Start from a named precision and fidelity policy (the
earlier stage's if it is faster and correct, otherwise the starting point in
[matmul and precision](../ttnn-knowledge/topics/matmul-precision.md#precision-and-fidelity)) and tune one tensor
group at a time: attention weights, MLP/expert weights, KV cache, activations and residuals, CCL payloads, norms,
logits, layer exceptions.

- Precision decisions use real weights and recorded input activations. Synthetic or random-tensor tests (even
  ones named "representative semantics") catch crashes, shape and replay bugs, and explosions; they cannot make a
  slower, higher-precision policy "best correct" when the cheaper policy passes real-weight evidence. A
  synthetic-only failure is a discrepancy to debug ([OPT-012](#advice-gates)).
- When dense MLP or expert matmuls dominate decode, the BFP4/LoFi MLP trial is required in this stage, not deferred
  to `$datatype-sweep`: a BF16-activation/BFP8-weight baseline, a BFP4 gate/up (FF1/FF3) candidate and a guarded
  BFP4 down (FF2) candidate when the model can tolerate it.
- KV-cache dtype evidence includes a short-prefix case (about 32 cached tokens, then decode) and a per-user control
  at the largest batch in `batch_sizes` on the same inputs, not only a long-prompt layer PCC.
- Activation precision is swept per path: BFP8 activations for the attention path and for the MLP or MoE path
  separately, under the same topology, before any global BFP8 policy. If global BFP8 fails, find the failing path;
  if one path loses, keep a mixed policy with evidence.
- For dominant decode paths across devices, keep a small matrix crossing activation/CCL dtype, material weight
  dtype, fidelity and topology family. A rejected BFP8-activation, BFP4-weight or LoFi candidate names the
  residual/collective topology it ran under and whether it was tried on the best topology candidate.
- If a prior-good policy fails in new code, debug before discarding it: loader grouping, layout, KV update math,
  scale and transpose, whether the harness runs the same policy. For the KV cache compare shape and mapper as well
  as dtype, and the fill/update dtype contracts ([prefill and KV](../ttnn-knowledge/topics/prefill-kv.md)).
- A precision change followed by an accuracy failure is a policy question until controls isolate a defect
  ([numerics](../ttnn-knowledge/topics/numerics.md)). A PCC collapse is usually a bug: reproduce it minimally,
  use `$autodebug` with the failing command, logs, shapes, policy and first bad tensor, and `$autofix` once the bug
  is concrete. Reject the dtype only after model-visible precision loss, an exact op-contract blocker, or an
  unfixed bug with preserved evidence.
- On the complete full model, the focus stays on parallelism, tracing, sharding, data movement, configs and host
  boundaries; `$datatype-sweep` owns the final accuracy/performance frontier. This stage still tries targeted
  precision or fidelity changes (MLP gate/up BFP4, layer exceptions, KV/cache/CCL dtypes, fidelity) when decode is
  materially below a credible target and the decoder-layer roofline says precision could close it, validated on
  the same traced full-model token-out path. "That is datatype-sweep work" is not a reason to skip them.

## Tuning Ops

- Set `memory_config`, `program_config` and `compute_kernel_config` explicitly for important ops.
- A matmul or matmul group among the largest decode-time consumers gets a local program-config search per role
  (QKV, output projection, MLP gate/up/down, router, shared expert, routed expert gate/up/down, LM head), over core
  grid, `per_core_M`, `per_core_N`, `in0_block_w`, output subblocks and blocks, memory configs and compute kernel
  config, measured on traced warmed decode at real shapes. Leave a table per search: role, percent of decode time,
  config, traced latency, PCC or correctness, decision and reason. A dominant group without a search table means
  the pass is not complete.
- If `tt-perf-report` shows a one-tile-row decode matmul as DRAM-bound and not DRAM-sharded, a DRAM-sharded
  candidate is required; if it loses, record the attempt and why.
- Any material matmul left with `in0_block_w<=2` or an output subblock below 2 is called out in the final summary
  with everything tried to enable a larger legal value and why it failed.
- Fidelity is swept separately from dtype for each dominant projection group: at least the legal LoFi and HiFi2
  configs at the same dtype.
- A second, interleaved weight copy for short prefill chunks needs a recorded byte budget beside its before/after
  rows: DRAM free after weights and the KV cache at the advertised context with the shipped cache dtype, the copy's
  bytes over all layers, and the context or batch the copy costs. A copy adopted without it is a review finding.
- Use SDPA, FlashDecode and FlashAttention ops where the model fits their contracts.
- MoE: optimize the gate-selected active-expert path; dense all-expert execution is a debug baseline. On non-Galaxy
  systems start from the GPT-OSS `ttnn.sparse_matmul` path with score weighting and expert reduction. Expert
  placement (tensor-parallel or expert-parallel) comes from the multichip stage and is a measured choice
  ([MoE](../ttnn-knowledge/topics/moe.md)). If `nnz` is omitted, record why and the measured cost; if material, try
  to make the exact-count invariant hold and remeasure
  ([sparse matmul](../ttnn-knowledge/topics/matmul-precision.md#sparse-matmul-nnz)). Dominant sparse projection
  roles get their own `in0_block_w`, subblock and memory-placement sweeps; a candidate that hangs or fails PCC is
  rejected alone and the sweep continues after device recovery. An explicit `ttnn.DRAM_MEMORY_CONFIG` on a decode
  intermediate stays only with a failed L1 attempt and before/after evidence.

### Advice Gates

Each gate applies when its pattern is material in the measured path. The why and how for each is in
[performance advice](../ttnn-knowledge/topics/perf-advice.md); read the entry before working the gate.

- **OPT-001 packed QKV.** Separate Q/K/V matmuls on one post-norm activation, with no per-projection contract,
  get a packed candidate before split tuning. The final `tt-perf-report` shows one QKV matmul row and no
  split-repair cluster, or the report gives measured evidence or a precise blocker after adapting weight packing,
  head creation, memory configs, rank, padding and dtype/fidelity. Packed and split compare under the same traced
  harness, context, precision and checks.
- **OPT-002 SDPA and KV cache.** A change to QKV packing, head creation, rotary, cache update, page tables, cache
  dtype/layout or attention input memory config is compared with the strongest prior attention path at the same
  traced context, including the SDPA/FlashDecode row, KV dtype and layout, page-table shape and distribution, head
  layout, attention program config, chunk sizes, block size, `num_kv_heads`, output memory config and block
  topology. A local SDPA regression is acceptable only when named, quantified and outweighed by whole-decoder time.
  With a BF16 cache and material SDPA, a reduced-cache candidate (usually BFP8) is required, even when the
  functional baseline was BF16. A reduced cache from the prior correct path is kept while PCC passes; a BF16
  fallback needs a correctness failure, op-contract blocker or measured same-context win including SDPA and cache.
  Material SDPA gets an explicit, architecture-appropriate config measured against the default (the Wormhole
  starting config does not carry to Blackhole; read the OPT-002 entry); a kept default records the explicit config
  tried, its row and why it lost. Report: SDPA row, KV dtype/layout, attention config, reduced-cache result,
  decision. Not optimized while SDPA is among the largest regressions against the prior same-context path, a BF16
  cache lacks a reduced trial or blocker, or material SDPA runs a default config with no explicit candidate.
- **OPT-003 residual layout.** Norms and residual adds in DRAM interleaved need a measured sharded candidate or a
  precise blocker (op, shape, memory config, error or PCC, measured fallback cost). Report both norm rows and their
  program configs, residual-add output memory configs, the residual memory config before attention and MLP, and
  every `InterleavedToSharded`, `ShardedToInterleaved`, reshape or slice row at helper boundaries. Not optimized
  while norm rows are among the largest regressions against the prior same-context path.
- **OPT-004 DRAM-sharded geometry.** Measure at least the residual or intermediate shard-grid candidate, a
  larger-core candidate when legal, and the existing helper or default, precision-locked: a geometry sweep that
  silently changes precision, fidelity or weight layout from the best prior path or the mandatory policy is
  invalid. MLP and expert rows include the best legal reduced-weight policy (usually BFP4/LoFi); keeping higher
  precision needs that result and an exact blocker, and a synthetic-only PCC failure is not one. A small
  `in0_block_w` or a divisibility error is not a matmul-only blocker until a wider upstream shard or inert padding
  was tried. Report per dominant role: shape, dtype/fidelity, program class, core count or grid source, input
  shard shape in tiles, `in0_block_w`, `per_core_M`, `per_core_N`, output subblocks, input/output memory configs,
  bandwidth class, traced latency, correctness, decision. Not accepted while a dominant DRAM-sharded row is marked
  `SLOW`, shows low DRAM utilization, missing subblock or small `in0_block_w`, until a precision-locked sweep tried
  larger legal divisors (non-powers of two, and `16` when legal) or a precise L1, divisibility or op blocker is
  recorded.
- **OPT-005 logical batch.** Report logical batch, tile-padded rows, current-position shape, page-table shape and
  distribution, KV-cache shape, page block size, active cache slots updated and SDPA row time; compare SDPA only at
  the same logical batch and context. A candidate that changes the logical batch is not optimized for the target;
  keep it only as a labelled throughput experiment outside single-user signoff.
- **OPT-006 cumulative contracts.** Before each focused experiment, write the strongest prior candidate's
  contracts. The final report has a cumulative contract table: packed/fused projections and head path, attention
  weight dtype/fidelity, KV dtype/layout, logical batch versus padded rows, SDPA config and row, residual/norm
  layout and rows, MLP/expert dtype/fidelity and geometry rows, total device time, op-to-op gap, correctness,
  intentional deviations. A run that regresses another validated contract is local evidence, not signoff.
- **OPT-007 attention precision.** When QKV, Q/K/V, output or fused attention matmul rows are material, a
  real-weight BFP4 attention-weight trial with each plausible legal fidelity is required before accepting BFP8 or
  BF16, tested independently of activation, cache, CCL and MLP dtypes and remeasured on the lowest-op-count
  compatible topology before the final decision. Report QKV/output dtype and fidelity, weight memory config, row
  times, SDPA/cache side effects, correctness, follow-on cache-use evidence when cache PCC is the concern, and the
  decision. Not optimized without that trial or a precise op or correctness blocker.
- **OPT-008 row-parallel output projection.** Compare local-input/full-output plus reduce or reduce-scatter with
  gathered-input/local-output (often fused all-gather+matmul). Report the selected decomposition, weight sharding
  per candidate, fused-op rank and layout requirements, CCL transfer count, program config, row times, whole-layer
  latency, correctness and the exact blocker of each rejected decomposition. A fused all-gather-matmul L1 failure
  reports requested and available bytes, shapes and program config, and the smallest legal local-output candidate
  is tried before the family counts as blocked.
- **OPT-009 persistent CCL buffers.** Repeated decode collectives get a persistent or preallocated candidate
  measured against the best non-persistent one in the same traced window, or an exact limitation. Report every
  material decode CCL row: payload dtype, input/output memory configs, axis/dim, topology, `chunks_per_sync`,
  `num_workers_per_link`, `num_buffers_per_channel`, persistent output and intermediate buffer status, row time,
  decision.
- **OPT-010 MLP gate/up.** With both families legal, packed gate/up is compared with the best fused-elementwise
  split path, holding dtype/fidelity, input memory config, residual layout, elementwise and row-parallel output
  contracts fixed. Report packed and split rows, program configs, input/output and weight memory configs,
  dtype/fidelity, split/slice/layout rows, activation and binary rows, downstream down-matmul and collective rows,
  whole-MLP and whole-layer latency, correctness, decision; an impossible family names its exact blocker.
- **OPT-011 working shards.** A dominant group blocked by "shard inner tiles must be divisible by `in0_block_w`"
  needs a measured wider working-shard candidate or a precise blocker. Report boundary and candidate working memory
  configs, shard shapes in tiles, legal and rejected `in0_block_w` and subblock values, exact errors, added layout
  rows, row times, whole-layer latency, correctness, decision.
- **OPT-012 synthetic vetoes.** A faster policy that passes real-weight PCC, replay and runtime checks is rejected
  only for model-visible output, generation or readiness failure on target weights, a real-weight trace or runtime
  failure, a final default that does not win, or an exact op-contract blocker. Report real-weight PCCs, synthetic
  PCCs if run, traced latencies and which evidence decided. Do not pass a suite by marking the cheaper synthetic
  case an expected failure while selecting the slower fallback.
- **OPT-013 dtype reached the op.** Before accepting a reduced-precision candidate, each dominant matmul row in
  `tt-perf-report` (or an equivalent profiler artifact) shows the policy's input, weight and output dtype and math
  fidelity; mismatches are debugged before timing. The report includes the rows that prove the kept policy.
- **OPT-014 precision times geometry.** Report the cross-product: per material geometry candidate, dtype/fidelity,
  core or grid source, input shard shape, `in0_block_w`, `per_core_N`, row and whole-layer latency, correctness,
  decision, or the exact blocker. Do not mix dtype policies to declare a geometry optimized. A dominant row marked
  `SLOW`, with low DRAM utilization, no subblock, `in0_block_w<=2` or a larger-grid blocker gets a precision-locked
  smaller-core or residual-grid candidate before acceptance.
- **OPT-015 Blackhole DRAM readers.** On Blackhole builds with multi-reader support, every material decode
  projection is measured with one reader and each legal two- and three-reader candidate under the same shape,
  layout, dtype, fidelity, geometry, firmware, trace and workload, at the production precision pairing, first as an
  isolated alternating-order microbenchmark and then integrated in traced layer and full-model timing. An isolated
  win that regresses the integrated path is rejected. The chosen count is explicit in the per-role configuration;
  the generic default stays one.

### Working Through Limitations

A TTNN limitation or bug is a problem to solve, not a reason to drop the optimization. Whether the fix may change
the op itself depends on the run's `scope` (`model-bringup`): under `release`, solve it inside the model; under
`open`, changing or copying the op is allowed, with a test at model shapes. When L1 buffers overlap,
read the op, its shapes and configs and reduce the L1 need ([memory](../ttnn-knowledge/topics/memory.md)); when a
shape is unsupported, find another op; when an op cannot pad, change the model contract so tensors are padded at
setup or on device (never with torch in the runtime path). If a failure crosses several ops, kernels, layouts or
runtime boundaries and progress stalls, use `$autofix`. A blocker is recorded with a small repro or the exact
failure.

### `tt-perf-report` Advice

For every actionable recommendation in the report: try it; record before/after latency, PCC and any watcher or
correctness issue; keep it when it improves the target without unacceptable PCC or complexity cost; reject it only
with evidence and the reason. Keep advice enabled in the report that guides optimization. Advice that seems wrong,
incomplete or misleading is recorded as a candidate `tt-perf-report` improvement.

Before finishing non-vLLM work, review a current report. An applicable optimization that is still untried is
tried, and a failure is debugged; a known optimization is not left for a later stage unless another skill owns it.
Untried applicable advice is reported as remaining work. vLLM serving stages review the serving benchmark and
contract evidence instead and create no profiler run for this.

## Prefill Latency And TTFT

The decoder, multichip and full-model optimization stages improve prefill too.

- Record warmed prefill latency before and after at the relevant logical lengths: a short prompt, a long prompt,
  and chunk boundaries and tails when chunking. Report physical chunk or bucket sizes and chunk counts beside the
  numbers, and keep first-use compilation and capture separate from warmed execution.
- At full-model and serving stages, also measure host TTFT on the same request workload and state the boundary
  (input preparation, all chunks, final norm and LM head, sampling, first-token readback).
- A faster matmul or chunk counts only if the complete prefill/TTFT target improves without an unexplained decode
  regression.
- Use reduced non-serving profiles to see whether time goes to projections, attention and cache ops, layout
  conversions, collectives or dispatch between chunks; serving stages use benchmark JSON only.
- For slow prefill matmuls, record per-device `M x K x N`, logical versus padded work, dtype/fidelity, active
  compute cores, memory placement and kernel time. Start large projections from a tuned 2D matmul and compare legal
  grids, K blocks, output blocks and subblocks and precision under the same real weights and activations,
  including packing, splitting, reshards and producer/consumer layouts.
- Leave a per-chunk-size kernel selection table for each prefill projection role: rows for 64, 128, 256, 1024 and
  the largest chunk; columns for each kernel family tried (1D multicast config, 2D config, `minimal_matmul`,
  interleaved copy), device time and the kept choice. Non-matmul prefill ops (norms, head split and concat, RoPE,
  activation) get the same treatment when material at short chunks. Kernel facts (the DRAM-sharded config is
  decode-only, `minimal_matmul`, per-bank readers):
  [matmul and precision](../ttnn-knowledge/topics/matmul-precision.md#program-configs-by-phase). Keep the
  one-reader decode control and measure the prefill path before adopting a reader setting.
- Chunking follows [the functional-decoder guidance](../functional-decoder/SKILL.md#implement-chunked-prefill).
  Compare feasible chunk sizes on the same logical request (smaller chunks bound memory but add dispatch, weight
  traffic and prefix reads; larger ones compute better but need more memory and pad more). Keep repeated-request
  correctness and prefill-to-decode cache behaviour. When dispatch dominates, consider traced chunk variants with
  `$tt-enable-tracing`.

Batched prefill belongs to optimized-vLLM (stage 10), not optimized full model (stage 7): stage 6 proves one
`prefill_forward` call with mixed rows equals the solo result per row; stage 7 keeps that contract and calibrates
bucket sizes against measured single-row prefill; stage 10 runs the dense work once for all short rows (equal padded
length or packed rows), keeps attention and cache fill per row, warms every bucket before the first capture, and
proves batched equals solo and a burst TTFT gain over the serial path with single-user latency unchanged. Design and
gates: [prefill features](../vllm-integration/references/prefill-features.md).

## Performance Accounting

Every optimized non-vLLM decode result reconciles three numbers from the same run:

1. Theoretical roofline: bytes the measured path moves per token (weights at stored dtypes plus KV-cache reads)
   divided by the aggregate DRAM bandwidth of the chips used.
2. Device-time decode: per-token device time from your own signposted `tt-perf-report` window.
3. End-to-end decode: warmed measured ms/token from the host.

End-to-end = device time + dispatch gap + host work. Remove avoidable non-device terms before accepting the result;
"the device math is fast but the loop is slow" is unfinished work, and a large gap usually means an untraced path,
per-step synchronization, host readback or input refresh. Name a ttnn/runtime/API limitation only after the
targeted fix was tried and evidence shows it blocks the path; name it precisely, it feeds the ttnn backlog. The
achievable roofline fraction varies by architecture, so the explanation, not a fixed percentage, is the requirement.

For a complete model or serving path, also write `doc/<stage>/perf_summary.json`. vLLM serving stages set
device-time fields to `null` and name the reason, for example `vllm_serving_profiler_disabled_to_protect_hardware`.

```json
{
  "workload": {"profile": "single_user_decode", "prompt_len": 128, "gen_len": 128, "batch": 1},
  "ttft_ms": 0.0,
  "decode_ms_per_token_e2e": 0.0,
  "decode_ms_per_token_device": 0.0,
  "roofline_ms_per_token_estimate": 0.0,
  "named_limitations": ["..."]
}
```

From a reduced-layer profile, `decode_ms_per_token_device` is the per-layer decode window times the layer count
(summed per layer kind) plus the terminal path (final norm, LM head, sampling) from the same profile; the host gap is
`decode_ms_per_token_e2e` minus that figure. State the derivation and the variant beside the number.

## Full-Model Decode Closure

First compute a target budget from the best decoder-layer evidence:

- `layer_stack_ms = sum(layer_count[kind] * optimized_multichip_decode_ms[kind])`;
- `layer_stack_tps = 1000 / layer_stack_ms` for batch-1 single-user decode;
- `full_model_overhead_ms = measured_full_model_ms_per_token - layer_stack_ms`.

If the layer stack already misses the target, return to decoder optimization before generator work. If it meets the
target and the full model does not, optimize the overhead before the mathematical core: final norm, LM head, logits
movement, sampling trace, token/position/RoPE/page-table refresh, trace replay blocking, synchronizations, host
readbacks, cache management, CCL buffer lifetime. The steady-state loop uses persistent device tensors,
`tt_out_tok` feedback, device-side position advance for fixed-step decode, and page-table copies only when the
page table changes.

A decoder optimization disabled in the full model because the stack hit L1, semaphore, trace or CCL limits is not
an accepted fallback until the resource was reduced or pooled (persistent CCL, output and ring buffers, semaphores,
trace inputs, page-table buffers). If it still cannot fit, record the exact failure and the measured cost.

Keep the multichip decoder's data-layout contract across the stack: no layer-to-layer all-gather added to simplify
the wrapper. Try fused collective/matmul or sharded-output patterns first; `$autofix` helps with bugs here.

### LM Head And Sampling

The terminal path (final norm, LM head, logits movement, sampling, token feedback) is part of optimized decode.
Knowledge for this section: [LM head and sampling](../ttnn-knowledge/topics/perf-advice.md#lm-head-and-sampling).

- Profile a reduced full-model token-out trace with the terminal path and measure its terms separately from the
  layer stack.
- The LM head is a real decode matmul: on more than one device split it over the mesh along vocab; keep the hidden
  stream sharded into final norm and LM head; use `LMHead1D` when it fits (otherwise as the template), the intended
  weight dtype, DRAM-sharded or ring program configs and output memory config. No single replicated full-vocab
  BF16 matmul as the default. Pad weights when needed for a legal, fast vocab-sharded matmul and keep the real
  vocab size separately for sampling.
- Logits reach the sampler in its expected layout with no full-vocab all-gather in the hot path. Padded vocab IDs
  are masked with very negative values in each local shard before force-argmax or local TopK.
- A slow single-core `TopKDeviceOperation` is not an accepted final path: measure the padded power-of-two width
  against the op's own routing and keep the faster.
- Use a common sampling implementation: compare `models/common/sampling/` with
  `models/common/modules/sampling/sampling_1d.py`, record the choice, trace it (or a generator-owned wrapper) and
  pass `tt_out_tok=<persistent decode token input tensor>`. Keep trace keys distinct for greedy, penalties and
  log-prob modes; warm and capture the active mode before measuring.
- Greedy decode uses the vocab-sharded split-sampling path; do not replace it with full-vocab all-gather plus global
  argmax because the split path is slow; fix the split path. The split-sampling greedy benchmark is semantically
  greedy, not a generic sampled `top_k=32` or top-p path. If greedy split sampling fails on sampler shape, layout or
  tiling, fix that contract or keep a minimal repro and leave the stage incomplete.
- If `ArgMaxDeviceOperation`, full-vocab all-gather, generic `TopKDeviceOperation` or sampling replay dominates
  token-out decode, fix the LM-head/sampling contract before retuning decoder dtypes or CCLs; the stage is not
  complete with it in the measured path.
- vLLM reuses the same terminal path: no adapter-side host argmax, full-logits readback or fallback sampler. Serving
  stages prove this with same-harness benchmark JSON and contract checks, reusing non-serving terminal profiles.

Before/after comparisons use the same measured path. A teacher-forcing or device-logit replay number does not show
a token-out generator or vLLM path is fast unless it includes the same sampling and feedback; record both when they
differ.

## Evidence To Leave

These items MUST be completed:

- Functional checks pass against the optimized path; prefill and decode PCC stay at the functional bar, with any
  material delta explained.
- Paged KV-cache and warmed trace replay still behave correctly; the runtime fallback audit is clean.
- Stress or repeated-run coverage appropriate to the risk of the changes.
- Warmed prefill and decode latency before and after optimization.
- `tt-perf-report` output with advice enabled and the main conclusions, from representative decoder/module tests or
  a reduced full-model variant, never a full all-layer trace (not in vLLM serving stages).
- Watcher clean with `TT_METAL_WATCHER=10`, asserts not skipped. The watcher battery covers every prefill and decode
  path this stage changed (continuation at nonzero start, lead rows, block-size groups, multi-row prefill). A
  watcher assert inside a tt-metal op the model only calls is reproduced in isolation and recorded as a tt-metal
  item with the repro path, and the stage ships a watcher-clean alternative; shipping the asserting path needs an
  operator waiver in the work log. In vLLM serving stages, a watcher run of the changed path on a reduced-layer
  generator probe covering every prepared shape satisfies this (the reduced test fixture caps its warmup buckets,
  so the pytest selection alone does not reach the batched row counts); never attach watcher to the live server.
  Remote Ethernet, ARC or ERISC errors during watcher or profiler runs: no further profiler collection, keep
  compact evidence, recover per `$tt-device-usage`, resume the same stage.
- vLLM decode-serving optimization: same-harness primary single-user and CI serving-burst before/after metrics;
  proof the measured path used on-device sampling without host greedy argmax or full-logits readback; a record that
  no Tracy, `tt-perf-report`, live-server or serving-adapter profiler collection was attempted.
- Optimized-full-model and vLLM-serving optimization: `$qualitative-check` evidence for the shared prompt suite after
  the selected optimization, with HF or previous-stage controls.
- Optimization checklist:
  - [ ] Decoder path fully traced with no host fallbacks.
  - [ ] Decode activations generally width-sharded in L1 across norm, attention, residual, MLP and output projection.
  - [ ] Prefill activations generally DRAM interleaved; 2D matmul program configs for large prefill matmuls.
  - [ ] Operation-topology audit recorded: op sequence, repeated same-input matmuls, collectives, reshard/layout
    conversions, fused or lower-movement candidates, dtype/fidelity constraints, action taken.
  - [ ] Multi-device topology candidates measured as coherent families where applicable (residual layout,
    collective placement, fused CCL+matmul, projection packing, activation/CCL dtype, persistent buffers). A
    rejection measured only under an incompatible residual/layout contract does not complete this item.
  - [ ] Lower-movement residual candidates measured without an immediate old-contract restore; if a reduce-scatter
    or fused CCL+matmul path only lost after an immediate all-gather or replication, a stack-compatible
    sharded/fractured path was measured or a minimal repro shows the next op cannot consume it.
  - [ ] Best-candidate comparison: the final path beats the strongest correct baseline, the earlier optimized
    artifact when present, and this stage's material candidates on the targeted prefill/TTFT or traced decode
    workload, with tradeoffs in the other phase reported. A synthetic-only precision veto is not a correctness
    reason when real-weight evidence passes; a geometry sweep under a different dtype/fidelity does not reject the
    final policy.
  - [ ] Final default performance reproduced the selected candidate under the final code path; if slower, the
    report uses the final number and explains why.
  - [ ] Final dtype/fidelity policy verified in the measured runtime rows (OPT-013). If a row shows BF16 or BFP8
    where the policy claims BFP4, the policy did not reach the op and the stage is incomplete.
  - [ ] SDPA and other optimized composite ops used instead of hand-built attention where the model fits.
  - [ ] Repeated same-input projections (Q/K/V, gate/up, model-specific groups) packed where legal and faster. Kept
    separate only with measured evidence or a specific TTNN/runtime blocker after adapting layout, rank, padding,
    weight packing and output splitting; kept packed only when it beats a well-tuned separate candidate after
    counting split, activation, binary and layout overhead.
  - [ ] Explicit `memory_config`, `program_config` and `compute_kernel_config` for important ops.
  - [ ] Every largest-decode-time matmul or group: legal program configs swept per role (core grid, larger legal
    `in0_block_w`, output subblocks and blocks, memory configs, compute kernel config) with a before/after table or
    an exact TTNN/runtime blocker.
  - [ ] Decode fidelity swept per dominant projection group: legal LoFi and HiFi2 at the same dtype on real traced
    decode; the fastest correct policy kept (BFP8 does not imply HiFi2).
  - [ ] Attention projection dtype/fidelity swept separately from MLP when attention rows are material; BFP8 or BF16
    attention names the real-weight BFP4 candidate tried and its correctness, latency or op blocker (OPT-007).
  - [ ] Dense MLP or expert matmuls among the largest consumers: BFP4/LoFi trials for gate/up (FF1/FF3) run, down
    (FF2) BFP4 tried or rejected with PCC/runtime evidence; ordered against prefill work by the latency target.
    These are candidates; `$datatype-sweep` makes the final choice.
  - [ ] Shard specs and core grids divide tensor dimensions cleanly into tiles where possible; grids as large as
    the model and hardware allow.
  - [ ] DRAM-sharded decode matmuls. On Blackhole builds with multi-reader support, every material role measured
    with one reader and each legal two- or three-reader candidate; the final count explicit per role; the generic
    default stays one.
  - [ ] Collective topology minimized: avoidable gather, reshard, all-reduce, reduce-scatter and all-gather removed,
    moved to cheaper boundaries, or justified with before/after evidence.
  - [ ] Fused matmul-CCL candidates (all-gather-matmul, matmul-reduce-scatter) tried wherever a collective and a
    matmul are or can be made adjacent, and kept when they measure faster and correct. A rejection includes an
    adapted attempt, not only the first API error.
  - [ ] Repeated decode CCLs use persistent or preallocated buffers where the API supports it, or the reason and
    measurement are recorded.
  - [ ] MoE: routed active-expert path optimized with `ttnn.sparse_matmul` where the model and hardware fit, correct
    `nnz` handling, separate gate/up and down tuning, correct sparse-input handling, routing-score weighting, expert
    reduction, no dense all-expert runtime path, no avoidable DRAM round trips through decode intermediates.
  - [ ] LM head and sampling: terminal path in the optimized token-out path and profiled separately in non-serving
    evidence; LM-head weights padded when needed; padded vocab IDs masked before force-argmax or TopK; TopK widths
    padded where the measured routing makes padding win; avoidable `ArgMaxDeviceOperation`, full-vocab all-gather,
    generic `TopKDeviceOperation`, host argmax and full-logits readback removed, or the stage stays incomplete
    until a minimal repro or lower-level fix exists.
  - [ ] LM head uses DRAM-sharded matmuls if present.
  - [ ] Reduced precision/fidelity experiments for this stage run and documented with real weights and input
    activations; for complete full-model top-k tuning the final frontier is `$datatype-sweep`'s.
  - [ ] Performance accounting reconciled (roofline, device-time, end-to-end from the same run; avoidable gaps
    removed; remaining gaps named only after a targeted fix attempt); `perf_summary.json` written for a complete
    model or serving path. vLLM serving stages use same-harness single-user and serving-burst metrics with
    device-time fields `null` and the no-profiler reason.
  - [ ] Batch capability preserved: target workload and batch 1 both measured; larger-batch or concurrent-serving
    correctness tested up to the largest batch in `batch_sizes` where hardware and memory allow.
  - [ ] Optimized full model: the multi-row prefill contract still equals the single-row output per row, every
    prefill bucket is warmed before capture with program-cache counts recorded, and prefix-cache resumed prefill
    still passes `run_prefill_resume_check` on the optimized path.
  - [ ] Optimized vLLM: batched prefill runs the dense work once for all short rows, batched output equals the
    single-row output per row, the `prefill_burst` TTFT ratio against the serial path meets its gate, and primary
    single-user TTFT and TPOT are within 5 percent of the pre-change values.

If the checklist is not complete, go back and do those steps. Decoder and module work focuses on on-device
performance; complete-model and serving work also drives host orchestration, synchronizations, readbacks and
input refreshes out of the measured path where the runtime contract allows.

## Final Audit Checks

- No unnecessary `InterleavedToSharded`, `ShardedToInterleaved`, `reshard`, `tilize`, `untilize`, `to_torch` or
  `from_torch` in the optimized runtime path.
- Decode trace replay measures the optimized path, not a fallback.
- After the final buffer-lifetime, persistent-buffer, trace-key, sampling or CCL changes, representative repeated
  replay runs with `TT_METAL_TRACE_ALLOC_TRACKING=1` as `$tt-enable-tracing` specifies. An unresolved unsafe
  allocation, an unexplained corruptible-buffer acknowledgment, or acceptance evidence that skips program-cache
  allocations leaves the path incomplete.
- Generator-wide preparation precedes capture; cross-request trace IDs and capture/retirement counts meet the
  lifetime and reuse evidence of `$tt-enable-tracing`, which also governs exact-tensor acknowledgments and minimal
  allocation scopes. Deferred first-capture cost counts in request latency.
- Program and compute-kernel configs are in the final report or a compact structured summary.
- If `supports_async_decode=True` is advertised, the split vLLM path ran: `decode_forward(read_from_device=False)`,
  `read_decode_output(async_read=True)` and `process_decode_output_host`.
- On-device sampling returns device tokens/logprobs through decode; host top-1 or argmax fast paths are removed or
  proven unused by the measured benchmark.
- PCC covers prefill and decode for every representative layer kind.
- Optimized stress runs and passes for every representative layer kind and exercised mode; skipped stress is not a
  passing result.
- Final perf reports cover warmed prefill and decode separately, except in vLLM serving stages.
- Watcher-clean evidence exists for the optimized correctness run.
