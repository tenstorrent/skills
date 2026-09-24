---
name: golden-tests
description: Prepare Stage 0 model analysis and cached real-weight PCC tests with semantic adapter contracts; preserve the test baseline in later stages.
---

# Golden Tests

Follow [model-bringup startup](../model-bringup/SKILL.md#startup). Stage 0 creates
the baseline below; later stages consume it without regenerating the reference.

## Stage 0

1. Read the target PyTorch implementation, resolved HF config and the supplied
   model workload requirements file (not a Python dependency requirements.txt).
   Record source paths/revisions and weight revision. Inventory each semantic
   decoder layer type, its count and layer indices, including attention/cache,
   dense/MoE and other behavior-changing variants. Counts must cover the stack.
   Separate index-dependent cache ownership/sharing or state behavior even when
   the Python class is identical; record why chosen indices represent each group.
2. Extract required input/output sequence lengths (ISL/OSL) and valid pairings.
   Use batch size 1. Keep all pairs up to ten; otherwise select ten representative
   pairs covering short/long inputs and outputs and relevant model boundaries.
   Preserve specified pairings rather than inventing a Cartesian product. Record
   the complete list, selected cases and selection rationale. If the requirements
   file or its length semantics are missing/ambiguous, ask; do not invent lengths.
   Prefer required pairs that exercise non-aligned tails and decode across a cache
   window/page boundary. Record uncovered boundaries rather than claiming coverage;
   later implementation-specific boundary checks supplement this ten-pair baseline.
3. Run the PyTorch reference in eval mode with real checkpoint weights and
   tokenized real text from a recorded corpus/sample, respecting the required ISL.
   Capture real prefill and decode activations from that model for at least one
   real layer index per semantic type; do not substitute random tokens/activations.
   Save inputs, outputs, masks, positions and full-model logits for the reference
   continuation covering the requested OSL. Record the exact tokens and decode-step
   convention; teacher forcing replays that same trajectory in later comparisons.
   Stream/chunk large captures to disk. Preserve required tensors and positions;
   do not hold every layer's full-context activations/logits in RAM simultaneously.
   Save real K/V produced by reference prefill as the decode fixture, plus expected
   K/V after prefill and at the end of decode. Never use random/zero populated-cache
   substitutes; an empty cache is valid only before an uncached prefill.
   For recurrent/linear-attention types, capture and check their actual persistent
   state instead of fabricating K/V; record the state tensors in the same manifest.
   Export compatible `readiness_v1` references from saved logits/tokens when the
   existing readiness workload matches. Record that metadata; distinct AIME/chat
   requirements remain separate references, generated once and reused unchanged.
4. Write parameterized PCC tests under `models/autoports/<model>/tests/golden/`
   and a separate explicit generation command. Cover both prefill and decode for
   every selected pair and layer type, with separate output PCC verdicts. Decode
   must consume and update its real prefill cache throughout the trajectory.
   Test isolated decode from the saved reference cache and prefill-to-decode using
   the implementation's own cache; never replace it with goldens between steps.
   Layer tests replay captured inputs entering that layer at each decode step;
   full-model tests replay recorded tokens through the complete implementation.
   Compare final K and V separately against cached reference values after prefill
   and after decode, per cache-owning layer. Reconstruct logical token/head order
   from paged/sharded storage; compare all valid retained positions, including the
   prefix and newly written tokens, excluding only padding or model-defined eviction.
   Exercise populated permuted pages and nonzero positions through the public API.
   Assert the permutation is nonidentity when multiple pages exist; use a distinct
   new decode token at the next absolute position, not a replay over prefilled state.
   Where the trajectory crosses a window wrap, compare subsequent outputs too;
   do not manually roll pages or repair state to supply behavior missing in the port.
   Define test adapter interfaces for decoder eager/traced execution (stages 1–3),
   reconstructed multi-chip outputs (4–5), full-model/logit outputs (6–8), and the
   serving adapter's numerical path (9–11). Reuse goldens across equivalent
   formats; each later stage implements its backend adapter. Keep adapters separate
   from fixtures/assertions; missing TTNN
   implementations must fail clearly when selected, never pass via a CPU fallback.
5. Store tensors outside Git under `bringup/references/<model>/golden/`. Write
   `tests/golden/manifest.json` following [the manifest and gate contract](references/manifest.md),
   including inventory, case/stage mapping, provenance and artifact hashes. Use
   PCC >= 0.995 unless the supplied model contract specifies otherwise; check
   shapes and finite values and use an explicit equality/tolerance check for
   constant tensors where PCC is undefined. Never silently skip a required case.
6. Leave a lightweight analysis handoff in the existing manifest and
   `doc/golden_tests/README.md`: layer inventory/representative indices, checkpoint
   mappings, source pointers and unusual model behavior such as shared KV, sliding
   windows, extra norms or recurrent state. Document adapter inputs/outputs, logical
   shapes, masks, positions and state ownership/update semantics. Map cases to the
   behavior they check. Do not create a decoder skeleton or a separate architecture
   specification; Stage 1 owns implementation structure and TTNN choices.
7. Summarize the supplied requirements in the README: its source path and the
   high-value workloads, features, evaluations, and external-tooling inputs for
   later stages to consider. This is context, not a decoder plan or a claim
   that every target is a Stage 0 gate.
8. Validate the fixtures with a thin CPU adapter under `tests/golden/` wrapping
   the original PyTorch layers, not a second handwritten model implementation.
   Replay every selected case from real weights and captured inputs/cache against
   outputs and state saved from the original full-model run. Never return saved
   expected values as computed results. This checks fixture extraction and the
   semantic interface, not independent model correctness or TTNN execution.
   Later stages select their own backend explicitly; no automatic CPU fallback.

Leave `doc/golden_tests/README.md` and `work_log.md` with generation and per-stage
test commands and case-to-behavior mapping. Run the packaged Stage 0 gate: CPU
parity plus deliberate wrong-output, wrong-state, wrong-position, missing-golden
and bad-digest controls. No device is needed for this validation. The
golden loader must work with reference generation disabled; CPU adapter execution
is a separate explicit validation, never implicit golden regeneration.
Resource limits are blockers to record, not permission to truncate ISL/OSL.

## Later stages

Treat this baseline as correct. Run the mapped tests against the actual delivered
stage path for every selected case and layer type, reporting prefill/decode output
and final K/V PCC separately. Cached PyTorch is the acceptance reference at every
stage; TTNN-vs-TTNN comparisons are additional diagnostics. Required traced-decode
PCC comes from replay outputs/state, across the recorded trajectory, not just its
last token or an aggregate that hides a failing step. Retain existing top-k, text,
performance and release checks. Served text is not a numerical PCC substitute.
Load cached goldens; missing artifacts or provenance/hash mismatches fail with the
explicit generation command, never regenerate inside a test. Do not fit outputs,
replace goldens with TTNN results, lower thresholds, drop cases or hide failures
with skips/xfails. Adapters may convert layouts and reconstruct logical state,
but must not supply missing model computation or repair incorrect state. Reuse the
analysis handoff; matching semantics does not require matching internal code
structure. Each stage owns its implementation and backend adapter.

Read the Stage 0 requirements summary and the relevant source requirements when
choosing later workloads, features, benchmarks, evaluations, or tooling. Use
them as priorities and record material tradeoffs; the batch-one golden matrix is
a numerical baseline, not the whole serving or release workload.

The runner executes the packaged golden gate before advancing each stage. Use its
fresh pytest results and provenance, not a handwritten pass claim. Zero tests,
collection errors, missing cases and skips fail the gate. Read
[the test/metric contract](references/manifest.md#tests-and-metrics) when implementing adapters.

Before changing a test or reference, record a minimal reproducer proving a test
defect against the pinned PyTorch source or requirements, why the correction is
needed, and the affected cases in `doc/golden_tests/work_log.md`. A TTNN mismatch
alone is not evidence. Preserve the old manifest/digests, regenerate only affected
goldens explicitly, and rerun affected stages. Stage review checks that evidence
and the test/manifest diff before accepting the change.
