---
name: dit-bringup
description: "Starts and drives a new end-to-end bring-up of a model in models/tt_dit (diffusion transformer, VAE, text encoder, or a full image/video pipeline) from a Hugging Face repo ID or a local model package, through seven fixed stages on the target Tenstorrent hardware: component bring-up, unit correctness, missing ops, end-to-end pipeline, output quality (VBench/CLIP), end-to-end performance, and optimization. Each stage ends with its result plus a short summary or report a person can review. Defaults to a Blackhole LoudBox (2x4 mesh). Use when starting or resuming a whole-model bring-up, or to run one stage or a range of stages on a model that is already partly brought up; for one component or one phase, use the tt-dit-* skill for that phase. Not for autoregressive text LLMs (use tt-model-bringup)."
---

# tt_dit model bring-up

Runs a bring-up as a fixed sequence of stages. Each stage has a required result, a gate, and a
human-readable summary. The technique for each stage lives in the phase skills; this skill owns
the order, the gates, and what gets written.

| Skill | Used for |
|---|---|
| `tt-dit-add-model` | Porting components, state-dict conversion, correctness gates |
| `tt-dit-kernel-research` | Finding or adapting an op before writing one |
| `tt-dit-benchmark-profile` | Warm device time, Tracy capture, reading profiles |
| `tt-dit-performance` | Optimization levers, re-checking quality every change |
| `tt-dit-loop` | Running stage 7 as rounds against a fixed baseline |
| `dit-device-backend` | Running every device command on the target hardware |

Before stage 1, read `references/porting-notes.md` and `../../shared/device-hangs.md`.

## Inputs (stage 0)

Resolve each input before any device work and record it in `<run-root>/run.json`. On resume,
read `run.json` and do not ask again.

| Input | Values | If missing |
|---|---|---|
| Model source | HF repo ID, or a local package path | Ask |
| Stages to run | All (default), one stage, or a range, e.g. `5`, `3-4`, `6-7` | Run all remaining stages |
| Existing port | Path to code already written for this model, if any | Look in the target directory; see "Partly brought-up models" |
| Weights | Real checkpoint path, or none yet | Ask; if none, run stages 1–4 on seeded random weights and mark the run as such |
| Closest in-tree model | The tt_dit model the port will copy patterns from | Propose one from `../../shared/reference-models.md`, confirm with the user |
| Target hardware and mesh | Default: Blackhole LoudBox, 2x4 mesh (`references/hardware.md`) | Use the default if `tt-smi` on the device host shows a matching system; otherwise ask |
| Production shape(s) | Resolution, frames, steps the model ships with | Infer from the model's configs; ask if unclear |
| Target directory | Default `models/tt_dit/` in the tt-metal checkout | Use the default |
| Device backend | `local`, `batch`, `slurm` | See `dit-device-backend` |
| Run root | Persistent directory for stage outputs | Ask; under `batch`/`slurm` it must be on storage the device host can read |

Resolve tt-metal, Python, and reference-library versions from the checkout's current
instructions, not from these skills. Record them in `run.json` and keep them across resumes.

Stage 0 gate: `run.json` complete and the backend health check passes on the target hardware.

## Stages

Run in order. Do not start a stage until the previous gate passes, unless the stage's
prerequisites are met as described in "Partly brought-up models". Details, prerequisites,
gates, and what each summary must contain are in `references/stages.md`.

| # | Stage | Required result | For a person to review |
|---|---|---|---|
| 1 | Component bring-up | Every component ported and running on the target hardware | `SUMMARY.md` with the model summary: similarities and differences vs the closest in-tree model |
| 2 | Unit correctness | A unit test per component, passing strict PCC and relative-RMSE bars on device | `SUMMARY.md` with a per-component accuracy table |
| 3 | Missing ops | Each missing op implemented and tested on device; host fallbacks removed | `SUMMARY.md` listing each op, approach, and accuracy |
| 4 | End-to-end bring-up | The full pipeline runs on the target mesh and matches the reference | `SUMMARY.md` with e2e accuracy and sample outputs |
| 5 | Output quality | VBench (video) or CLIP score (image) at or above the reference's | `SUMMARY.md` with the score table vs the reference |
| 6 | End-to-end performance | Warm device time at the production shape, Tracy profile, perf report | `PERF_REPORT.md` ranking the tall nails |
| 7 | Optimization | Tall nails from stage 6 addressed under `tt-dit-loop` until a stop gate fires | `PERF_REPORT.md` before/after, plus `SUMMARY.md` |

Stage outputs go in `<run-root>/stages/<NN>-<name>/`. Formats are in
`references/stage-summary.md`.

## Partly brought-up models

Any stage or range of stages can be run on its own, including for a model that was ported
before this workflow existed. Follow `references/partial-runs.md`. In short:

1. Run stage 0 as usual, plus an inventory of what already exists (code, tests, outputs,
   earlier stage summaries) recorded in `run.json`.
2. For each earlier stage, adopt rather than redo: if it has a summary, trust it; otherwise run
   that stage's existing checks on device and write an `adopted` summary of what they show.
   Do not rewrite working code to fit this workflow.
3. Run the requested stages. Stop at the end of the range.

A missing prerequisite the requested stage cannot run without (for example, no end-to-end
pipeline before stage 6) stops that stage: report what is missing and which stage produces it.
Anything weaker (an earlier gate failing or never checked) is flagged under "Needs review" and
the requested stage continues.

## Rules

- **Gates run on the target hardware.** Gate evidence must come from a run through
  `dit-device-backend` on the target hardware type, with the log path, mesh shape, and commit
  recorded. CPU, simulator, or a different device type can help while developing but never
  pass a gate.
- **Every stage writes its summary before its gate is marked passed.** A stage without a
  summary is not done.
- **Flag, don't block, on accuracy.** A missed default threshold does not stop the run: look
  for a bug, measure the precision floor, then continue with the measured bar and list it under
  "Needs review" (`references/stages.md` stage 2). Never loosen a bar silently.
- **Do not go back silently.** If a later stage finds a bug in an earlier one, fix it, re-run
  that stage's gate, and add an amendment to that stage's summary.
- At the end, write `<run-root>/BRINGUP.md` (one page linking every stage summary) and
  `<run-root>/bringup_report.json`.
- Record what each stage learned that would help the next port: model-neutral pitfalls go in
  `../../shared/known-issues.md`; never record details of a closed-source model there.

## Closed-source packages

When the model source is a local package rather than a public HF repo, treat its contents as
confidential:

- Load the reference from the package path; never upload or fetch it elsewhere.
- Keep config values, layer names, and shapes out of commit messages, PR text, shared notes,
  and anything outside the run root and target directory.
- Write to the target directory the user names; do not assume `models/tt_dit/`.
