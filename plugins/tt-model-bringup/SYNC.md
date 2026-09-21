# Source and publication boundary

This is a deliberate import of the model bring-up workflow from
`tenstorrent/tt-metal`, branch `agentic-research/fast-models-fast`, commit
`70a596f92229ada922fba743cd0cd9d2658a5c1c`. `sync-source.json` records the source path,
Git blob, original SHA-256 and packaged SHA-256 for each imported file. The persistent log/resume change
`7a90015bcee` is included, as are context-contract checks and per-layer-kind real-weight coverage.

The canonical workflow now lives inside this plugin. Future imports need deliberate review of
upstream changes, the package adaptations and the publication boundary; there is no runtime
checkout lookup or automatic prompt sync. Do not import AutoDebug skills, hidden experiment data,
generated reference tensors, auth/config files, machine logs or private workflow state.

Package adaptations:

- Add the model-bringup entrypoint, visible dependency declaration and explicit installation-root
  validation; resolve runner skills from the package and the selected AutoDebug dependency.
- Move readiness tools to a `readiness_check` Python namespace exposed through startup PYTHONPATH.
  Use lazy package exports so offline gates need no model dependencies. Input book/AIME corpora
  remain explicit target-checkout inputs; generated references go in its bringup workspace.
- Resolve check scripts from their installed package, preserving the target checkout as cwd.
  Confirm enabled package paths through the live Codex skills inventory before launching a turn.
- Remove personal commentary and private-project acceptance pointers. Add setup references to
  individually selectable skills. Replace site-specific path examples with operator-selected paths
  and scope Docker cleanup to the recorded run container. Preserve stage goals, checker exit
  meanings and resume logic.

The plugin contains instructions and verification support; TTNN runtime fixes remain in tt-metal.
No accelerator validation is implied by host tests. Model/replay/hardware evaluations belong in a
separate deliberate change with model selection, cost limits and hardware ownership established.

## Validation commands

The existing `pytest tests/` CI job collects the no-model packaging tests and imported offline
runner/output-check regressions. It needs only pytest and PyYAML. Prompt discovery continues after
independent failures under pytest; the existing CI timeout bounds the job. No API keys, model calls,
TTNN imports or hardware runners are added to PR CI.

For the additional CPU-side readiness contract tests, use a separate environment with torch,
transformers, requests and openai, then run from this repository:

```bash
PYTHONPATH=plugins/tt-model-bringup/runtime HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  python -m pytest plugins/tt-model-bringup/runtime/readiness_check/
```

These tests use synthetic generators and tokenizers; they do not download weights or open a device.
Actual PCC, Tracy, vLLM/TTI and hardware validation remain separate deliberate work.

## Serving compatibility correction (0.1.1)

The serving CLI and TT configuration interface target `tenstorrent/vllm` commit
`5ffebf4128f81ea5cf8413175eabde52cd8c8d75` (`dev` head checked on 2026-09-08).
`vllm/engine/arg_utils.py` registers `--additional-config`; the TT plugin's
`config.py::get_tt_config` reads its `tt` object. Use that interface rather than the
obsolete `--plugin-config` flag. Serving and generator readiness now share mesh labels,
including the QB2's `P300x2` (1 x 4 chips).

Stages 9 and 10 require served qualitative artifacts (`--scope vllm`), so earlier
full-model autoregressive output cannot satisfy these gates. The serving skill also
requires verification of page-growth refresh and pending-token handling before async
overlap; the pinned dependency does not provide the previously claimed guarantee.
These changes do not include a vLLM scheduler patch or imply hardware validation.

## Benchmark stage replacement (0.1.6)

Stage 11 now uses pinned lm-eval tasks through native-chat vLLM, fixed content-identified
subsets, and 4K-input performance at concurrency 1 and 32. TTI release remains a
standalone skill, with its former prompt's handoff guidance in the skill and its
artifact checker under `skills/tti-release/scripts/`. It has no multigoal prompt.
Original source hashes remain in sync-source.json while packaged hashes describe
the adapted imports; the benchmark runtime and prompts are new files. New runtime
helpers live in benchmark_stage. The calibration report records measured coverage,
runtime, reference agreement and hardware limitations; package tests alone establish none.
