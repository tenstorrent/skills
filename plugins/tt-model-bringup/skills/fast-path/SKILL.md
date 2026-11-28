---
name: fast-path
description: Make a served TTNN model as fast as Blackhole allows on QuietBox 2 (one, two or four chips) once vLLM serving works. Covers the decode roofline, a DRAM-streaming fused batch-1 decode built from generic_op kernels for each mesh, fused multi-row and batched decode, faster prefill, opt-in deterministic prefill and opt-in speculative decoding, each behind exactness, accuracy and release gates. Use after optimized-vLLM, or to speed up an already served model.
---

# Fast path

Batch-1 decode on Blackhole is bound by DRAM bandwidth: every token reads every active weight once.
A stock TTNN decode step spends most of its time in op launches, small single-core glue ops and
matmuls that stream far below the ~512 GB/s one Blackhole chip can read. One served MoE model
measured 18% of that peak on four chips. This skill replaces the decode step with a few
`ttnn.generic_op` programs that stream weights from all DRAM banks at once, fuse the work between
them and keep the next token on the device. It then extends the same programs to several users,
speeds up prefill, and adds two opt-in serving modes: bit-reproducible prefill and speculative
decoding.

On gpt-oss-20b (QuietBox 2) this took one user from 88 to 306 tok/s on four chips, 73 to 187 on
two and 49 to 106 on one. 32 users went from 1,176 to 1,753 tok/s in total, and 4k-token time to
first token from 506 to 392 ms, with full-set GSM8K and IFEval inside their gates. See the
[case study](references/case-study-gpt-oss.md) for the measured history and what transfers to
other models.

## Before you start

- The model serves through `models/autoports/<model>/tt/generator_vllm.py` with stage 10 evidence
  (or you work on the model code of a published package and its harness). Its context contract
  (`doc/context_contract.json`) and serving contract are the floor this skill preserves.
- Teacher-forced reference logits from the HF model exist for a fixed set of positions (the
  stage 6-8 readiness references, or a new set of at least 512 positions over natural text).
- The authorized hardware is a QuietBox 2 (four Blackhole chips: meshes 1x1, 1x2, 1x4) or one
  Blackhole card. The kernels assume Blackhole DRAM banks and a compute grid of at least 10x9;
  the method (roofline, streaming, fusion, gates) transfers to other devices, the kernels do not.
- Read the reference implementation in the target checkout: `models/demos/gpt_oss/tt/fused_decode/`
  (README, `stream.py`, `boundary.py`, `terminal.py`, `inputs.py`, `kernels/`). The
  [case study](references/case-study-gpt-oss.md) links the one-chip, two-chip, multi-row, batched,
  prefill and speculative versions.
- $tt-device-usage governs chips, resets and quiet-box timing. $tt-enable-tracing governs trace
  preparation and lifetime. $optimize stays the authority for stock-op tuning; this skill starts
  where stock ops stop.

## Rules

1. **Roofline first.** Compute bytes per token for each mesh and format, the DRAM floor, and the
   measured share of peak before writing a kernel ([roofline](references/roofline.md)). Rank levers
   by bytes saved or bandwidth gained. A speed claim without its bytes is incomplete.
2. **The stock path stays.** Every condition the fast path does not cover keeps the original
   decode: other meshes, widths, penalties, log-probs, grammar masks, unsupported sampling. Route
   explicitly per step; never let an unsupported request reach a fused program.
3. **Three kinds of change, three proofs.** A refactor or speedup that keeps the math (prefill
   kernels, reader splits, padding) must leave output digests bit-identical. A change of numerics
   (fused decode, formats, multi-row reductions) must pass the accuracy gates against the HF
   reference and the stock path. Speculative decoding must produce exactly the text normal decoding
   produces. See [validation](references/validation.md).
4. **Opt-in when it trades something.** A path that costs another workload (a two-chip decode
   that slows eight users, speculation, deterministic prefill) ships behind a profile switch with
   its trade-off stated. Defaults never get slower.
5. **Do not reduce capability.** Decode-only weight copies cost DRAM. Keep the KV pool and the
   context contract unless a hard physical limit forces a reduction; record the largest feasible
   value and the evidence.
6. **Traces live for the server's lifetime.** Build every fused program, constant and buffer and
   warm every prefill bucket before the first capture. A program compiled after capture is a defect
   to fix, not a reason to recapture.
7. **Report what the bytes say.** Put GB per token, achieved GB/s per chip and % of peak next to
   every step time. Compare against another implementation only at matched weight formats, or say
   which formats differ. Pair quality evaluations on identical prompts on the same day.

## The fast-path track

Run the [fast-path goal templates](../../prompts/model_bringup_fast_path) with the bring-up runner
after stage 10 and before stage 11, so the benchmark measures the final path (see
$model-bringup). Each goal writes `models/autoports/<model>/doc/fast_path/<phase>.json`, checked by
`${TT_MODEL_BRINGUP_ROOT}/scripts/check_fast_path.py`, and needs a $stage-review `clean-pass`.

| Goal | Builds | Gate (details in [validation](references/validation.md)) |
|---|---|---|
| 1 Roofline | bytes per token, floors, measured baseline per mesh, DRAM budget, ranked plan | accounting consistent; baseline not below the floor |
| 2 Streaming decode | fused batch-1 decode on the primary mesh, served as the batch-1 bucket ([guide](references/streaming-decode.md)) | faster; accuracy within 0.5/0.3 pt top-1/top-5; KV pool kept; traces clean |
| 3 Mesh variants | the other meshes: one chip, two chips ([guide](references/mesh-variants.md)) | per mesh: fused, opt-in or stock, with numbers or a reason |
| 4 Multi-user decode | fused multi-row decode for 2-8 rows, streamed batched decode for 8-32 ([guide](references/multi-user-decode.md)) | per width faster; accuracy per width; ladder rows ≥ 97% |
| 5 Prefill | bit-identical TTFT work, opt-in deterministic prefill ([guide](references/prefill.md)) | TTFT not worse; digests equal; determinism proven |
| 6 Speculative decoding | cost model, then build or close ([guide](references/speculative-decoding.md)) | shipped: ≥ 1.10× measured, identical output; closed: numbers |
| 7 Acceptance | full matrix against the previous release, report tables | ladder, quality, c=1 subsets, smoke, condition matrix |

Goals 3 to 6 are independent once goal 2 lands; run them as separate branches and merge each with
the fast checks in [validation](references/validation.md). A goal may end `closed` with measured
reasons; it may not end without evidence.

## Which path serves a request

Keep this table in `doc/fast_path/README.md` for the model, filled with what shipped:

| Condition | Fast path | Falls back to |
|---|---|---|
| one running request, greedy or top-k ≤ 32, no penalties/log-probs/grammar | fused batch-1 decode on the profile's mesh | stock decode bucket 1 |
| 2-4 (or 8) running requests | fused multi-row decode at the next prepared width | stock buckets |
| 8-32 running requests | batched decode streaming the union of routed experts (MoE) or all weights (dense) | stock buckets |
| penalties, log-probs, grammar masks, top-k above the candidate width | stock sampler on stock logits | — |
| prefill | the package's prefill (bit-identical speedups) | — |
| deterministic prefill requested | opt-in all-reduce variant (profile switch) | default prefill |
| speculative profile, one greedy request, prompt within the warmed range, no prefix-cache hit | draft, check and accept in one trace | plain fused decode, exactly |

## Phase guides

- [Roofline and baseline](references/roofline.md): accounting, floors, budgets, and this skill's
  calculator `scripts/fast_path_roofline.py`.
- [Streaming decode](references/streaming-decode.md): the program set, kernel rules, decode-only
  weights, sampling, serving as a bucket, per-op and per-layer checks.
- [Mesh variants](references/mesh-variants.md): one chip without weight copies, two chips, what
  changes per tensor-parallel width.
- [Multi-user decode](references/multi-user-decode.md): weights read once for M rows, expert union,
  exact rows, L1 budgets, the cost fit, batched streaming; `scripts/expert_union.py`.
- [Prefill](references/prefill.md): bit-identical prefill kernels, padding, host routing stalls,
  warmup, and the opt-in deterministic all-reduce.
- [Speculative decoding](references/speculative-decoding.md): when it pays (dense vs MoE), the cost
  model `scripts/spec_cost_model.py`, the exact check, the drafter, the upstream plugin contract.
- [Validation and release gates](references/validation.md): evidence files, thresholds, digests,
  quiet-box timing, same-day pairing.
- [Pitfalls](references/pitfalls.md): failures that cost days on the reference model.

## What transfers to another model

- **Dense models** stream every MLP and attention weight; the boundary, input and terminal
  programs carry over almost unchanged. Their multi-row and speculative checks are close to free
  (every row reads the same weights), so goals 4 and 6 usually pay more than on MoE.
- **MoE models** stream only the routed experts per token. Several rows pull in the union of their
  experts, so multi-row and speculative steps cost more per row; measure the union first.
- **Per-model work** is the attention variant (sinks, sliding window, head counts, RoPE scaling),
  the router (softmax before or after top-k, shared experts, routing bias), norms and activations
  (clamps, gates), tied embeddings and vocabulary padding. Adapt the kernels, then prove each op
  against the stock op before the layer, and each layer before the model.
- **Formats:** keep the formats the datatype sweep selected. A lower-bit format is a new
  accuracy question, not a free speedup: on gpt-oss-20b BFP4 experts were 20% faster and failed
  the accuracy gate on every quantizer tried.

## Done

The model's `doc/fast_path/` holds the seven evidence files with passing checks, the README with
the condition table and the bytes and bandwidth table, and a work log. Every fast path has a
profile switch and a default. Stage 11 or the TTI release then measures the result, and the
packaging owner ships it (for tt-model packages, follow the `tt-model-fast-path` skill in
`tenstorrent/tt-model-manager`).
