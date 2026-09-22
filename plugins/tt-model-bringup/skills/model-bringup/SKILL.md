---
name: model-bringup
description: Implement Hugging Face text models in TTNN through staged decoder, multi-chip, full-model, datatype, vLLM and benchmark work. Use for model bring-up or resuming its staged goals; requires separately installed tt-autodebug.
---

# Model bring-up

Own the implementation, stage gates and evidence for one target HF model in a tt-metal checkout.
Read the relevant stage skill and its exact goal before doing that stage. Do not skip unmet gates
or replace acceptance criteria with a review plugin's criteria.

## Startup

1. Resolve this plugin root from this file: two directories above its skill directory. Set
   `TT_MODEL_BRINGUP_ROOT` to that absolute path. Keep the installed package read-only.
2. Verify `tt-autodebug` is installed **and enabled** in the current host using its plugin inventory.
   This dependency is declared in [dependencies.json](../../dependencies.json). If missing, stop
   and give the appropriate explicit install instruction:
   - Codex: `codex plugin add tt-autodebug@tenstorrent-skills`
   - Claude Code: `/plugin install tt-autodebug@tenstorrent-skills`
   Never install it automatically or use a stray source checkout in place of an enabled plugin.
3. Resolve `TT_AUTODEBUG_ROOT` from that installed plugin's skill path (two directories above the
   `autodebug` skill directory). Validate the package and prepare the shell environment:

   ```bash
   # Set these two paths from the host's enabled installation inventory first.
   export TT_MODEL_BRINGUP_ROOT="<installed tt-model-bringup directory>"
   export TT_AUTODEBUG_ROOT="<installed tt-autodebug directory>"
   BRINGUP_EXPORTS=$(python "$TT_MODEL_BRINGUP_ROOT/scripts/environment.py") || exit
   eval "$BRINGUP_EXPORTS"
   ```

   Run this setup in shells used for model work. The runner also supplies these exports to its
   child. `PYTHONPATH` exposes the packaged `readiness_check` module; it does not copy files into
   tt-metal. Shell commands using package resources must quote the expanded absolute paths.
4. Confirm the HF model/revision, target checkout, acceptance contract and already-authorized
   hardware. Resolve model directory as `models/autoports/<lowercase HF ID with non-alphanumeric
   characters replaced by underscores>`. Do not acquire hardware just because a plugin was installed.
5. Keep the checkout, `CODEX_HOME`, runner logs and artifacts on persistent storage. Runtime model
   changes belong in the target checkout; workflow fixes belong in this plugin. Record exact
   commands, commits, hardware identity and evidence. Keep weights, credentials and private logs
   out of published commits.

## Execution

The eleven [goal templates](../../prompts/model_bringup_multigoal) run in order:

1. Functional decoder — `functional-decoder`
2. Fused decoder — `graph-fusing`
3. Optimized decoder — `optimize`
4. Multi-chip decoder — `multichip`
5. Optimized multi-chip decoder — `optimize`
6. Full model — `full-model`
7. Optimized full model — `optimize`
8. Datatype sweep — `datatype-sweep`
9. vLLM integration — `vllm-integration`
10. Optimized vLLM — `optimize`
11. Benchmarks — `benchmark-model`

For an optional standalone TTI release handoff once serving is ready, use
[`tti-release`](../tti-release/SKILL.md).

`tt-device-usage`, `tt-enable-tracing`, `qualitative-check` and `stage-review` provide shared
requirements. AutoDebug/AutoTriage/AutoFix come from the explicit dependency. Independent stage
review must return `clean-pass`; findings require repairs and rereview. Preserve original goal
criteria, local commit boundaries and the prohibition on pushing stage changes automatically.

### Validate changed paths

When a stage adds or changes chunking, padding, sharding, dispatch, or memory ownership,
select a small set of checks from the actual branch conditions and allocation rules.
Cover each affected path, the valid lengths immediately around its boundaries, and
awkward tails; one large passing input does not cover a different branch or remainder.
Check sibling consumers of the same rule (for example, attention and MLP). Use cheap
host-side shape/allocation arithmetic where useful, then exercise the affected device path.

Record the changed path, selected cases, and results in the existing stage evidence.
A previous stage's pass does not validate a path introduced or changed later. Rerun the
affected cases on the final implementation; reuse unaffected evidence. Start diagnosis
with an op or representative layer, but preserve the stage's required full-model and
serving acceptance checks. Do not add a full boundary sweep or long soak by default.

For an unattended Codex run, install [requirements.txt](../../requirements.txt) in the active
Python environment, or provide `--codex-bin` for an existing Codex with goals/app-server support.
The templates authorize skill-requested subagents. Inspect the full expanded goals and execution
permissions before starting a costly run. The runner defaults to `--approval-policy never`
and `--sandbox danger-full-access`; override these for environments that require narrower access.
It uses the supplied Codex home's authentication and removes ambient OpenAI/Codex API keys.

From the target tt-metal checkout, first perform a no-model dry run:

```bash
python "$TT_MODEL_BRINGUP_ROOT/scripts/multigoal" \
  "$TT_MODEL_BRINGUP_ROOT"/prompts/model_bringup_multigoal/*.txt \
  --repo "$PWD" --replace HF_MODEL=org/model \
  --replace MODEL_DIR=models/autoports/org_model --dry-run
```

The dry run validates package contents without starting Codex. Before the first live goal, the
runner verifies both packages are enabled in the selected Codex home through `skills/list`. It
binds short workflow skill names to their installed, qualified plugin names and paths.

Inspect the expanded prompt copies and manifest under `bringup/artifacts/multigoal-runs/`.
Remove `--dry-run` to execute the reviewed goals; choose the requested model with `--model` and
reasoning effort with `--effort`. This runner requires Codex. Claude can execute individual stage
skills and check scripts directly, preserving the same stage order and acceptance criteria.

To resume a stopped stage, use the same prompt selection/replacements, Codex home and preserved
`--log-dir`, adding `--resume-stage N`. Keep the original `--start-index` for that prompt selection;
use `--start-index N` only when the supplied first prompt itself is stage N. Resume reuses the
recorded thread and appends attempt logs. Do not restart earlier completed stages unnecessarily.

## Evidence gates

Stages 6, 7, 9, 10 and 11 have sibling `.check.sh` scripts. Run from the target checkout with
`MODEL_DIR` set to its exact autoport path. They resolve their own package resources. Exit 0 passes;
1 is advisory; 2 is critical; other exits mean checker/environment failure. The runner retries
checker errors once and stops by default; it runs bounded remediation goals for failed checks.
`--no-checks` and `--check-error-policy continue` are explicit overrides, not acceptance evidence.

The readiness runtime provides generator/serving contracts, HF reference generation, prefill,
traced teacher-forcing, autoregressive and vLLM runners. Use `python -m readiness_check.<runner>`
after startup. Hardware runners require the active tt-metal/TTNN environment plus torch,
transformers, requests and openai as applicable. Book/AIME input corpora are read from the target
checkout's documented `models/` paths; generated references belong under `bringup/references/`.
No reference tensors or experiment outputs are bundled.

Report stage status, commands, checked artifacts and remaining failures. Packaging tests and dry
runs prove wiring only; they do not prove PCC, text quality, device safety or performance.
