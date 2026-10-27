# Stages

Each stage: what to do, the gate, and what its summary must contain beyond the common header in
`stage-summary.md`. All device runs go through `dit-device-backend` and follow
`../../../shared/device-hangs.md`.

## 1. Component bring-up

Prerequisites: hard: stage 0 (reference model source loadable on the host). Soft: none.

Do: follow `tt-dit-add-model` Orient and Scaffold. List the components (text encoder(s),
denoiser blocks, conditioning, positional embedding, VAE encoder/decoder, scheduler glue). Port
each by copying the closest in-tree model's structure and reusing `layers/` and `utils/` (see
`porting-notes.md`). Where an op is missing, use a temporary host fallback and list it for
stage 3. Wire the parallel config for the target mesh.

Gate: every component runs a forward pass on the target hardware at a production shape without
error, with real or seeded random weights loaded through strict state-dict loading.

Summary adds: the component list with the in-tree module each reuses or subclasses; a model
summary of important similarities and differences vs the closest in-tree model; host
fallbacks still in place.

## 2. Unit correctness

Prerequisites: hard: the components to test exist and run on device (stage 1). Soft: none.

Do: write one test per component against the CPU reference with identical weights, following
`tt-dit-add-model/testing-and-accuracy.md` tiers 1–3. Use `assert_quality` so PCC and
relative RMSE come from one place. Test production shapes only.

Gate, per component, on device:

| Check | Default bar |
|---|---|
| Single layer or op | PCC ≥ 0.9999 where the op can run at the reference precision; otherwise ≥ 0.999 |
| Block | PCC ≥ 0.999 |
| Output used as an absolute value (VAE latents, pixels) | relative RMSE bar as well as PCC |
| Sharded vs unsharded TT path | Data parallel bit-exact (PCC = 1.0); spatial within the unsharded bar, seams checked |

These defaults are targets, not stop conditions. When a component misses one:

1. Look for a real bug first: bisect against the reference as in
   `tt-dit-add-model/testing-and-accuracy.md` "When a gate fails".
2. If none is found, measure the precision floor (for example, the same component with the
   reference in bf16), set the component's bar just below what it achieves, and continue.
3. List it under "Needs review" with the measured value, the floor, and what was tried.

Do not stop the bring-up for a missed default. The stage gate is that every component has a
test, runs on device, and either meets its default or is flagged with a measured reason.

Summary adds: a table of component, shape, mesh, PCC, relative RMSE, bar, pass/fail; any bar
below the defaults under "Needs review".

## 3. Missing ops

Prerequisites: hard: a list of missing ops or host fallbacks (stage 1 summary, or found by
reading the existing code). Soft: stage 2 tests for the affected components.

Do: for each host fallback from stage 1, follow `missing-ops.md`. Re-run the stage 2 tests of
every component that used the op.

Gate: no host fallbacks left on the device path (any that remain are listed under "Needs
review" with the reason), each new op has its own on-device test at the stage 2 single-op bar, and the
affected stage 2 tests still pass.

Summary adds: per op, the approach (existing-op change, composite, tt-lang kernel, new
kernel), where it lives, its accuracy, and whether it is a candidate to upstream.

## 4. End-to-end bring-up

Prerequisites: hard: every component exists (stage 1). Soft: stage 2 passed, stage 3 passed.

Do: assemble the pipeline following the closest in-tree pipeline. Run it on the target mesh at
each production shape with real weights (seeded random weights only if none exist yet, and
say so). Compare against the reference pipeline with the same seed and inputs, and add the
sanity and roundtrip checks in `testing-and-accuracy.md` tiers 4–5.

Gate: full-model forward PCC ≥ 0.99 against the reference, the decoded output is not degenerate
(not blank, not noise, no seams or flicker), and the run completes on the target mesh.

Summary adds: e2e accuracy table per shape; paths to sample outputs from TT and the reference
side by side; any difference a person should look at.

## 5. Output quality

Prerequisites: hard: a pipeline that produces output on device (stage 4) and real weights.
Soft: stage 4 passed.

Do: score the TT output and the reference output on the same prompts and seeds with VBench
(video) or CLIP score (image), following `testing-and-accuracy.md` tier 6. Needs real weights.
A missing scoring dependency is a failure to report, not a pass.

Gate: each score is at or above the reference's within the noise band recorded for that
metric. A shortfall is investigated (bisect by swapping TT components for reference ones);
if unexplained, it is listed under "Needs review" and the run continues.

Summary adds: a table of metric, TT score, reference score, difference; the prompts and seeds
used.

## 6. End-to-end performance

Prerequisites: hard: a pipeline that runs on the target mesh at the production shape
(stage 4). Soft: stages 4 and 5 passed, since a fast wrong model is not a result.

Do: follow `tt-dit-benchmark-profile`. Measure warm device time at the production shape on the
target mesh, capture a Tracy profile, and generate the ops report.

Gate: the measurement passes the validity gate in
`tt-dit-benchmark-profile/benchmark-and-profile.md`, and the profile and report files exist.

Report: `PERF_REPORT.md` as in `stage-summary.md`, with the ranked tall nails.

## 7. Optimization

Prerequisites: hard: a stage 6 `PERF_REPORT.md` for the current commit (run stage 6 first if
the code changed since), and an end-to-end correctness check that can be re-run. Soft: stage 5
passed; without it, quality is guarded only by the stage 4 check, and the summary must say so.

Do: run `tt-dit-loop` with the stage 6 measurement as its Phase 1 fixed baseline and the stage 4
and 5 gates as its quality gate. Work the stage 6 tall nails in order, using
`tt-dit-performance` and the optimization order in the plugin README.

Gate: a `tt-dit-loop` stop gate fires, and the stage 2, 4, and 5 gates (those that were run) still
pass on the final commit.

Report: `PERF_REPORT.md` with the baseline vs final table and a row per round, plus
`SUMMARY.md` with the stop gate that fired and the remaining levers.
