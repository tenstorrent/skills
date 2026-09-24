# Source and publication boundary

This plugin is the canonical source for the model bring-up workflow. Its skills,
prompts, runner and verification tools are maintained together inside this directory.

## Source tracking

`sync-source.json` records the source path, Git blob, source SHA-256 and packaged
SHA-256 for files imported from `tenstorrent/tt-metal`, branch
`agentic-research/fast-models-fast`, commit `70a596f92229ada922fba743cd0cd9d2658a5c1c`.
When editing an imported file, update its packaged hash and preserve its source
identity. Files authored in this plugin have no upstream source entry.

Upstream imports require review of both the source changes and the package contract
below. Keep generated reference tensors, credentials, machine logs and private
experiment state out of the package. TTNN model and runtime changes belong in the
target tt-metal checkout.

## Package contract

- `model-bringup` validates the installed plugin root and its separately installed
  `tt-autodebug` dependency. Runner skills resolve from these enabled packages.
- Startup exposes `readiness_check` and `benchmark_stage` through `PYTHONPATH`.
  Readiness exports are lazy so offline gates can run without model dependencies.
  Book/AIME corpora are explicit target-checkout inputs; generated references belong
  in that checkout's bringup workspace.
- Check scripts resolve resources from their installed package and run with the
  target checkout as their working directory. The runner checks enabled package
  paths through the live Codex skills inventory before launching a turn.
- Persistent runner logs support stage resume. Stage gates cover implementation
  identity, context contracts and real-weight coverage for each decoder-layer kind.
  Preserve stage goals, checker exit meanings and resume behavior when importing.
- Use operator-selected paths and limit Docker cleanup to the recorded run container.
- The benchmark runtime is in `runtime/benchmark_stage`; Stage 11 uses fixed lm-eval
  subsets through native-chat vLLM and 4K-input performance at concurrency 1 and 32.
- The standalone TTI release workflow, handoff guidance and artifact checker are in
  `skills/tti-release/`.

## Serving contract

Serving configuration uses `--additional-config` with a `tt` object. The interface
is defined in `vllm/engine/arg_utils.py` and the TT plugin's `config.py::get_tt_config`;
the compatibility reference is `tenstorrent/vllm` commit
`5ffebf4128f81ea5cf8413175eabde52cd8c8d75`. Serving and generator readiness use the
same mesh labels, including `P300x2` for QB2's 1 x 4 chips.

Stages 9 and 10 require served qualitative artifacts (`--scope vllm`). Verify
page-growth refresh and pending-token handling before enabling async overlap.

## Validation

The repository's `pytest tests/` suite uses pytest and PyYAML for package checks
and offline runner/output-check regressions. It checks prompt discovery and reports
independent failures without model calls, API keys, TTNN imports or hardware.

For CPU-side readiness contract tests, use a separate environment with torch,
transformers, requests and openai, then run from the repository root:

```bash
PYTHONPATH=plugins/tt-model-bringup/runtime HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  python -m pytest plugins/tt-model-bringup/runtime/readiness_check/
```

These tests use synthetic generators and tokenizers. Model accuracy and performance
require separate hardware measurements with recorded implementation identity,
protocol and sample coverage. Benchmark completion checks measurement integrity and
report completeness; accuracy acceptance belongs to the bringup owner.

## Trace allocation guidance (0.1.15)

Adapt the six-skill policy change from [tt-metal #54769](https://github.com/tenstorrent/tt-metal/pull/54769),
commit `0799df070a5b07583f4315550a8daed925be84cb`, to this plugin's canonical skill paths.
References and API examples were checked against [tt-metal #53735](https://github.com/tenstorrent/tt-metal/pull/53735),
merged as `c05eff453698efb2c992c078de21d1e3c8ed7036`, and main at
`b99aa035f391aa32466350090543bc0eabb026bb`.
The guide lives in the target checkout at
`tech_reports/AdvancedPerformanceOptimizationsForModels/TraceCorrectness.md`;
the public Python helpers live in `ttnn.tools.trace_allocation_tracker`.

Generator-wide two-phase warmup follows [tt-metal #42698](https://github.com/tenstorrent/tt-metal/issues/42698)
and the prepare/record split in [#55343](https://github.com/tenstorrent/tt-metal/pull/55343),
merged as `743890db568bd3ff9626166a2ae201c28aa35072`.
That implementation is a warmup-structure reference, not an endorsement of its
broad allocation scopes. Cross-request reuse and exact-buffer acknowledgment
requirements address [#51800](https://github.com/tenstorrent/tt-metal/issues/51800)
and [#57299](https://github.com/tenstorrent/tt-metal/issues/57299), including
cross-trace lifetime proofs and unexpected-allocation negative controls.

Only packaged hashes for the six edited skills change in `sync-source.json`;
the original import provenance stays intact. No tracker runtime code is vendored.
