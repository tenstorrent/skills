---
name: optimize
description: Optimize per-device performance of runnable TTNN code, preserving correctness while improving layout, precision, sharding, program configs, data movement, and warmed latency with tt-perf-report evidence except in vLLM serving stages, where profiler collection is intentionally skipped.
---

# Optimize TTNN code

Before using this workflow, follow the dependency and environment setup in
[the model-bringup entrypoint](../model-bringup/SKILL.md#startup).
Resolve its package root from this installed skill's location; do not assume
this plugin is copied into the target tt-metal checkout.


This skill assumes you have runnable TTNN code with passing correctness tests. If not, first use the appropriate bringup or debugging skill. This guide is written for autoregressive LLMs with prefill and decode phases. If the target model differs, map each requirement to the nearest equivalent path and record that mapping; do not drop correctness or performance evidence.

Optimize for the eval workload: the largest served batch (normally 32 users) generating long answers, with prefill latency and time-to-first-token (TTFT) as explicit targets alongside decode. Batch 1 stays a regression row and the FUNCTIONAL bar (1 user, 128 in/128 out, at least 10% of theoretical); do not regress it. Prioritize the measured bottleneck in the requested workload. Preserve larger-batch correctness: do not hard-code batch 1 into the optimized path, trace inputs, cache/page-table handling, sampling, or output formatting. For complete model and serving optimization, keep or add evidence that larger batches/concurrency still work, normally up to 32 when the target hardware, memory, and harness allow it.

Precision starts from BF16 activations and BFP8 weights. BFP4 (MLP, expert or attention weights) is a candidate measured in this skill and recorded for `$datatype-sweep`; stage 8 makes the final choice with the P1 reasoning-text precision probe. Do not ship BFP4 as the default from an optimize stage.

## How To Use This Skill

This file is the map. Each checklist line below links to its detail in `references/`. Open a reference when its item is in play, read the section you need, and do not re-read references you already applied in this stage. Work from the measured bottleneck: profile first, then open the reference for the top rows.

## Stop Rule

This is the one optimization stop rule for every optimization stage (2, 3, 5, 7 and 10); other skills and stage prompts link here. Optimization ends at a measured point, not when every idea is exhausted. Stop optimizing and write the final evidence when any of these holds:

1. Every checklist line below has a measurement, a kept/rejected decision, or an exact blocker.
2. The target workload is close to the hardware limit: traced decode reaches at least 70% of the roofline estimate from [performance accounting](references/perf-accounting.md), and the last two kept candidates each gained less than 3%. These are defaults; the stage brief or owner may set others.
3. The optimization time box is spent: by default 8 hours of device work in this stage, counted from the first candidate measurement.

When rule 2 or 3 fires, list every untried item in the work log under `Not tried (stop rule)`, with its expected gain when known. These are hand-off notes, not blockers, and a reviewer treats them that way. The stop rule bounds effort only and never waives correctness: functional checks, PCC, trace replay, watcher, the context contract, larger-batch correctness and the FUNCTIONAL bar (1 user, 128 in / 128 out, at least 10% of theoretical; a floor, not a target to stop at) must still pass. Wherever a reference says a stage "is not optimized" or "is incomplete" until a performance item is tried, read it as "until the item is tried or the stop rule fires".

## Checklist

Method and scope:
- Measure traced, warmed decode and prefill on real shapes, weights, dtypes and batch; keep the context contract and non-aligned lengths. [method](references/method.md)
- Do the operation-topology audit table before knob tuning; compare multi-device collective families as wholes. [method](references/method.md)
- Rank candidates against the best correct measured path; rerun the final default path with the same harness. [method](references/method.md)
- vLLM serving stages: no Tracy, `tt-perf-report` or device profiler; use same-harness benchmark JSON. [method](references/method.md)
- From optimized-full-model on, rerun the `$qualitative-check` shared suite on the selected path. [method](references/method.md)
- Profile full models with a reduced-layer variant (one real layer per kind); keep watcher and profiler runs separate. [method](references/method.md)

Measurement:
- Collect `tt-perf-report` with advice enabled; try or reject each actionable advice item with evidence. [perf-report](references/perf-report.md)
- Reconcile roofline, device-time and end-to-end decode from one run; write `perf_summary.json` for complete-model work. [perf-accounting](references/perf-accounting.md)
- Record prefill and TTFT before/after at short, long and chunk-boundary lengths, with a per-chunk-size kernel table. [prefill-ttft](references/prefill-ttft.md)

Matmuls, layout and topology:
- Sweep program configs for each dominant matmul role and leave a search table. [matmul](references/matmul.md), [OPT-004](references/opt-advisories.md#opt-004-sweep-dram-sharded-decode-matmul-geometry-not-only-dtype)
- Use DRAM-sharded decode matmuls; on Blackhole, sweep readers per bank against the one-reader control. [matmul](references/matmul.md), [OPT-015](references/opt-advisories.md#opt-015-sweep-blackhole-dram-readers-per-bank)
- Test packed QKV against split Q/K/V. [OPT-001](references/opt-advisories.md#opt-001-treat-split-decode-qkv-as-a-topology-defect-until-proven-otherwise)
- Keep the SDPA/KV-cache contract and try a reduced KV-cache dtype. [OPT-002](references/opt-advisories.md#opt-002-preserve-the-sdpa-and-kv-cache-contract-when-changing-attention)
- Carry the decode residual layout through norms and residual adds. [OPT-003](references/opt-advisories.md#opt-003-preserve-the-decode-residual-layout-through-norms-and-residual-adds)
- Keep logical batch separate from tile padding. [OPT-005](references/opt-advisories.md#opt-005-keep-logical-decode-batch-separate-from-tile-padding)
- Keep a cumulative contract table; sign off the integrated candidate. [OPT-006](references/opt-advisories.md#opt-006-sign-off-cumulative-optimized-contracts-not-isolated-wins)
- Compare row-parallel output projection decompositions. [OPT-008](references/opt-advisories.md#opt-008-compare-row-parallel-output-projection-decompositions)
- Make repeated decode CCL buffers persistent when legal. [OPT-009](references/opt-advisories.md#opt-009-make-repeated-decode-ccl-buffers-persistent-when-legal)
- Compare packed and split MLP gate/up. [OPT-010](references/opt-advisories.md#opt-010-compare-packed-and-split-3-matmul-mlp-gateup-in-decode)
- Try phase-specific activation shards when they unlock matmul geometry. [OPT-011](references/opt-advisories.md#opt-011-use-phase-specific-activation-shards-when-they-unlock-matmul-geometry)
- Cross precision with matmul geometry. [OPT-014](references/opt-advisories.md#opt-014-cross-precision-with-matmul-geometry)

Precision:
- Tune precision and fidelity one tensor group at a time from BF16 activations/BFP8 weights; compare LoFi and HiFi2; record BFP4 candidates for stage 8. [precision](references/precision.md)
- Measure attention-projection precision separately from MLP precision. [OPT-007](references/opt-advisories.md#opt-007-treat-attention-projection-precision-as-its-own-decode-search)
- Decide precision on real weights and activations, not synthetic stress. [OPT-012](references/opt-advisories.md#opt-012-do-not-let-synthetic-precision-stress-veto-real-weight-wins)
- Prove the selected dtype and fidelity reached the measured ops. [OPT-013](references/opt-advisories.md#opt-013-prove-dtype-policy-reached-the-measured-ops)

Full model:
- Close the full-model decode gap against the layer-stack budget; optimize the LM head, sampling and token feedback as decode work. [lm-head-sampling](references/lm-head-sampling.md)
- MoE: keep the routed active-expert `ttnn.sparse_matmul` path; use `nnz` only when it is exact. [moe](references/moe.md)

Evidence and hardware:
- Complete the evidence checklist and the final audit before finishing. [evidence-checklist](references/evidence-checklist.md)
- T3K/Galaxy profiler hazards and reset recovery. [t3k-galaxy](references/t3k-galaxy.md); general device rules are in `$tt-device-usage`.
