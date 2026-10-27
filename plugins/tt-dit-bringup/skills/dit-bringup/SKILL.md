---
name: dit-bringup
description: "Starts a new end-to-end bring-up of a model in models/tt_dit (diffusion transformer, VAE, text encoder, or a full image/video pipeline) from a Hugging Face repo ID or a local model package. Resolves the model source, target directory, mesh, and device backend (local, batch, or Slurm), then runs the bring-up under tt-dit-loop until it ends in a bring-up report with per-component tests, an end-to-end pipeline, and a Tracy profile. Use when starting a whole model from scratch; for one component or one phase, use the tt-dit-* skill for that phase. Not for autoregressive text LLMs (use tt-model-bringup)."
---

# tt_dit model bring-up

This skill starts a bring-up and defines what it must produce. The work itself is done by the
phase skills under `tt-dit-loop`:

| Skill | Role |
|---|---|
| `tt-dit-loop` | Runs the bring-up as a campaign of rounds; owns checkpoint, ledgers, resume, stop gates |
| `tt-dit-add-model` | Gets each component numerically correct against the reference |
| `tt-dit-benchmark-profile` | Measures warm device time and reads profiles |
| `tt-dit-performance` | Makes a correct component faster, re-checking quality every round |
| `tt-dit-kernel-research` | Checks whether an op exists or can be made to fit before writing one |
| `dit-device-backend` | Runs every device command on the right machine |

## Inputs

Resolve each input before any device work. Record them in `<run-root>/run.json` and in the
`CAMPAIGN.md` header (`tt-dit-loop` Phase 0). On resume, read `run.json` and do not ask again.

| Input | Values | If missing |
|---|---|---|
| Model source | HF repo ID, or a local package path | Ask |
| Target directory | Default `models/tt_dit/` in the tt-metal checkout | Use the default |
| Mesh | Device count and mesh shape | Detect through the backend, then confirm |
| Production shape(s) | Resolution, frames, steps the model ships with | Infer from the model's configs; ask if unclear |
| Device backend | `local`, `batch`, `slurm` | See `dit-device-backend` |
| Run root | The `tt-dit-loop` campaign root | Ask; under `batch`/`slurm` it must be on storage the device host can read |

Resolve tt-metal, Python, and reference-library versions from the checkout's current
instructions, not from these skills. Record what was resolved in `run.json` and keep it for
resumes.

### Closed-source packages

When the model source is a local package rather than a public HF repo, treat its contents as
confidential:

- Load the reference from the package path; never upload or fetch it elsewhere.
- Keep config values, layer names, and shapes out of commit messages, PR text, and anything
  outside the run root and target directory.
- Write to the target directory the user names; do not assume `models/tt_dit/`.

## Stages

Run these under `tt-dit-loop`: stages 1 and 2 are its Phase 0.5 and Phase 1 setup for
bring-up, and each later stage is one or more rounds. Do not start a stage until the previous
gate passes and its evidence is in the run root.

| # | Stage | Skill | Gate |
|---|---|---|---|
| 0 | Inputs, backend, versions | this skill, `dit-device-backend` | `run.json` complete; backend health check passes |
| 1 | Prior art and reference | `tt-dit-loop` Phase 0.5, `tt-dit-add-model` Orient/Scaffold | Reference imported, not hand-ported; frozen reference outputs saved |
| 2 | Components | `tt-dit-add-model` | Each component passes its unit gate and its e2e gate on real weights |
| 3 | Full pipeline | `tt-dit-add-model` | End-to-end test matches the reference within its quality bar |
| 4 | Multichip | `tt-dit-add-model` Parallelize | Sharded path gated against unsharded on the target mesh |
| 5 | Performance | `tt-dit-benchmark-profile`, `tt-dit-performance` | Fixed baseline recorded; Tracy profile and perf report saved; a `tt-dit-loop` stop gate fires |
| 6 | Report | this skill | `bringup_report.json` lists every artifact below as present and passing |

## Required artifacts

Paths are relative to the run root unless noted.

- `run.json`: inputs, resolved versions and commits, backend name.
- `CAMPAIGN.md`, `plan.md`, `lineage.jsonl`, `ledgers/`: as defined by `tt-dit-loop`.
- Per-component tests in the target directory, with their latest passing output.
- An end-to-end pipeline test in the target directory, plus the generated sample output.
- `artifacts/round-<N>/`: Tracy capture and the generated perf report for the final baseline.
- `bringup_report.json`: one entry per artifact above, with path and pass/fail, plus the
  `tt-dit-loop` final report's honest statement of whether the target was met.

## Device work

Run every device command through `dit-device-backend`. Do not call `tt-smi`, reset devices,
or launch device tests directly.
