---
name: train-bringup
description: Bring up and optimise training of a Hugging Face model in tt-train (ttml) on a single chip, or on the box the plan's multi-chip fit section authorises when the model does not fit one chip, through three phases, plan, composite bring-up, optimisation loop. Use for a new training bring-up, full fine-tuning or LoRA, or to resume one of its phases; requires separately installed tt-autodebug.
---

# Training bring-up

Own the implementation, gates and evidence for training one target HF model in a tt-metal checkout
with tt-train built. Single chip by default; the plan's hardware ceiling decides whether the model
needs the box, and the user authorises it. Read the phase skill and its exact goal before doing
that phase.
Do not skip unmet gates or replace acceptance criteria with a review plugin's criteria.

## Startup

1. Resolve this plugin root from this file: two directories above its skill directory. Set
   `TT_TRAIN_BRINGUP_ROOT` to that absolute path. Keep the installed package read-only.
2. Verify `tt-autodebug` is installed **and enabled** in the current host. If missing, stop and give
   the explicit install instruction:
   - Codex: `codex plugin add tt-autodebug@tenstorrent-skills`
   - Claude Code: `/plugin install tt-autodebug@tenstorrent-skills`
   Never install it automatically.
3. Resolve `TT_AUTODEBUG_ROOT` from that installed plugin's skill path and prepare the shell:

   ```bash
   export TT_TRAIN_BRINGUP_ROOT="<installed tt-train-bringup directory>"
   export TT_AUTODEBUG_ROOT="<installed tt-autodebug directory>"
   BRINGUP_EXPORTS=$(python "$TT_TRAIN_BRINGUP_ROOT/scripts/environment.py") || exit
   eval "$BRINGUP_EXPORTS"
   ```

4. Confirm the HF model/revision, the training recipe (full fine-tuning or LoRA, sequence length,
   batch, optimiser), the target checkout (tt-metal root with `tt-train/build` present, `TT_METAL_HOME`
   set to the checkout) and the authorised hardware (one chip, or the box and its TP/DP layout),
   together with the hard-constraints block (precision, batch, forbidden modes) that every phase
   reads before any benchmark. Model code lives under
   `tt-train/sources/ttml/ttml/models/<model>/`, the training example under
   `tt-train/sources/examples/<model>_train/`, evidence under `tt-train/bringup/<model>/`.
5. Keep the checkout, runner logs and artefacts on persistent storage. Record exact commands,
   commits, hardware identity and evidence. Keep weights and credentials out of commits.
6. Commands that change firmware state (TDP, TDC or clock limits; ARC messages such as a TDP-limit
   set) run under the user's shell, not the agent's: write the exact invocation into the plan or
   work log, ask once, and continue with the measured ceiling. Plan this split on day 1.

## Phases

The three [goal templates](../../prompts/train_bringup_multigoal) run in order:

1. Plan — `train-plan`
2. Composite bring-up — `composite-bringup`
3. Optimisation loop — `optimization-loop`

`tt-device-usage` and `stage-review` provide shared requirements; `autodebug`, `autotriage` and
`autofix` come from the dependency. Every phase ends with an independent `stage-review` that returns
`clean-pass`; findings require repairs and re-review. Commit phase-owned changes locally; never push.

Step time is the sum of five op classes, and only the first is the model: useful math, layout
overhead, glue overhead, wasted overhead, host/dispatch gap. The plan sets the hard constraints,
the hardware ceiling and the roofline target (wall ms/step and MFU at the nominal peak of the
authorised chips), the composite bring-up makes the model correct and measures the baseline, and
the loop takes the biggest math kernel first: iteration 1 is an own-kernel attempt on the top ttnn
op, and glue or layout work comes in only where it deletes a whole op chain. The loop ends when the
roofline target or the stated hardware ceiling is met, or every remaining item is a math kernel
proven near its own roofline (roofline figure, measured fraction, failed kernel attempt). A
stage-review clean-pass closes a phase only when that phase's gate is met; in the loop it is a
checkpoint every eight iterations or at a milestone, and the agent continues to the next iteration
in the same turn instead of reporting and stopping. The agent never stops to ask for scope: the
target and the hard constraints are the scope, and "try both" means both are built and measured.
The agent ends its turn only when the gate is met or it is blocked on a decision only the user can
make (a hardware limit, a constraint change).

## Execution

From the target tt-metal checkout, first perform a no-model dry run:

```bash
python "$TT_TRAIN_BRINGUP_ROOT/scripts/multigoal" \
  "$TT_TRAIN_BRINGUP_ROOT"/prompts/train_bringup_multigoal/*.txt \
  --repo "$PWD" --replace HF_MODEL=org/model --replace MODEL_DIR=tt-train/sources/ttml/ttml/models/org_model \
  --replace RECIPE="full fine-tuning, seq 2048, batch 1, AdamW" --dry-run
```

Inspect the expanded prompts and manifest under `bringup/artifacts/multigoal-runs/`. Remove
`--dry-run` to execute. The runner requires Codex; Claude Code executes the phase skills directly in
the same order with the same gates. To resume a stopped phase use the same prompts, Codex home and
`--log-dir` with `--resume-stage N`.

## Evidence

Every phase leaves a work log (`doc/<phase>/work_log.md`) with commands, commits, hardware, artefact
paths, and a README with results. The ranked cost table (`doc/perf_analysis.md`) is created in the
composite phase and updated on every loop iteration; it is the state of the bring-up.

Report phase status, commands, checked artefacts and remaining failures. Dry runs prove wiring only;
they do not prove PCC, loss behaviour, device safety or performance.
