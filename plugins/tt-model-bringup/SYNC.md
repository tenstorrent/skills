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
select the standalone `tenstorrent/vllm-tt-plugin` and the upstream vLLM version
recommended by its current installation instructions, recording the resolved pair per run.
Serving and generator readiness use the
same mesh labels, including `P300x2` for QB2's 1 x 4 chips.

Every local server launch rejects known legacy vLLM and bundled-plugin source layouts.
Sampling tests run from the source checkout that matches the installed plugin.
Version and commit verification remains part of setup and resume.

Stages 9 and 10 require served qualitative artifacts (`--scope vllm`). Verify
page-growth refresh and pending-token handling before enabling async overlap.

The full-model stage pins the serving pair and records the plugin interface in
`doc/full_model/serving_contract.json`. Changes to the plugin or vLLM checkouts follow
`skills/vllm-integration/references/plugin-changes.md`, and stage review checks them.

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

## Trace allocation guidance (0.1.19)

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
These historical sources explain the change. The operational skills describe the
process directly, without issue references or a pinned generator implementation.
Cross-request reuse and exact-buffer acknowledgment
requirements address [#51800](https://github.com/tenstorrent/tt-metal/issues/51800)
and [#57299](https://github.com/tenstorrent/tt-metal/issues/57299), including
cross-trace lifetime proofs. Use exact-tensor acknowledgments whenever possible.
Allocation scopes require a reason and the narrowest possible boundary around
reviewed allocation calls. No injected-allocation test is required.

The mixed-shape serving requirement covers the request-key and chunk-offset
failure in [#58202](https://github.com/tenstorrent/tt-metal/issues/58202).
Stages 9 and 10 require this evidence. The packaged runner does not yet automate
the workload or validate its trace events.

Normal serving must preserve traces for the configured model/device lifetime.
Correctness-driven invalidation is a design defect, not an accepted recapture path.
Explicit lifecycle transitions and deliberate bounded-cache designs are separate
from this serving requirement; they are not automatic exceptions to its checks.

Only packaged hashes for the six edited skills and two stage prompts change in `sync-source.json`;
the original import provenance stays intact. No tracker runtime code is vendored.

## Serving plugin change policy (0.1.20)

Bringup agents first read the serving plugin interface during vLLM integration, after the
generator, sampler and datatype policy were frozen. A plugin change was then the smallest
local diff, and the stage gates offered no stop for a missing plugin feature. The full-model
stage now pins the serving pair and writes a serving contract from the plugin's own interface
documents. `skills/vllm-integration/references/plugin-changes.md` defines who owns a serving
gap, which plugin changes need human authorization, and the justification record that stage
review checks.

`scripts/check_serving_contract.py` is authored in this plugin. The stage 6 and stage 9 gates
run it after the context contract check; it rejects a missing serving contract, an unpinned
plugin commit, and rows with status `missing`.

Only packaged hashes for three edited stage prompts, two edited check scripts, three edited
skills and `contract_vllm.py` change in `sync-source.json`; the original import provenance
stays intact.

## Prefix caching and batched prefill (0.1.21)

The skills gated prefix caching on "implemented and tested" without saying how to implement or
test it, and nothing in the runtime exercised multi-row prefill; the readiness "batch prefill
check" is a single-sequence all-position check. Served models therefore shipped with prefix
caching either off or on without proof, and one shipped model returns corrupted text on repeated
prefixes. Both features are now required for every text model, with two documented exclusions
(`block_output`, `recurrent_state`) that the serving contract must name.

`skills/vllm-integration/references/prefill-features.md` is authored in this plugin. It holds the
continuation invariants a cache hit, a chunked continuation and a preemption resume share, the
equal-length and packed-row batched-prefill designs, the warmup rule, the thresholds, and the
negative controls. The generator contract gains a `start_pos` keyword on `prefill_forward` and a
non-abstract `prefill_logits_continuation` driver. `runtime/readiness_check/run_prefill_resume_check.py`
compares resumed prefill with full prefill over a position window and requires its `no_history`
and `fresh_suffix` controls to fail. `run_vllm_server.py` gains `--enable-prefix-caching`,
`--server-log`, the opt-in `prefix_cache` and `prefill_burst` stages, and writes
`vllm_run_summary.json` on every run. `scripts/check_prefill_features.py` is authored in this
plugin and runs last in the stage 9 and stage 10 gates; stage 10 passes `--require-burst`.

Only packaged hashes for three edited stage prompts, two edited check scripts, six edited skills,
`contract.py` and `run_vllm_server.py` change in `sync-source.json`; the original import
provenance stays intact.

The second trial (Qwen3-8B from stage 1 on one chip, Ornith 1.5 9B stages 9 and 10) folded its
findings back in. The skills now require short-prefix and per-user cache-dtype evidence, a watcher
battery over every changed prefill path, a byte budget for a second weight copy and a per-chunk-size
kernel table, bounded device jobs with no SIGTERM mid-dispatch and operator-only resets, a review
closure rule for findings raised after the last allowed rereview, a measured memory view behind the
context contract, and a single-chip rule for skipped multichip stages. The resume gate reports a
pass under lowered thresholds as advisory, the serving-contract gate reports a missing evidence path
as advisory, the stage 9 gate runs the prefill-feature check after a serving-contract advisory,
`generate.py` accepts the transformers 5 chat-template return type, and the readiness mesh open takes
`READINESS_TRACE_REGION_SIZE`.

The stage 7 pass of that trial added: one stage split for batched prefill across the optimize skill and
the prefill-features reference (stage 6 proves the multi-row contract, stage 10 implements batching), the
DRAM-sharded matmul named as decode-only, the TopK routing rule read from the op source before padding,
the reduced-variant derivation of `decode_ms_per_token_device`, kernel selection by row count as a
resume-check failure cause, the timeout-first hang rule, HF-control reuse, the short-prefix and batch-32
cases for a cache-dtype candidate, and `run_prefill_resume_check --output-on-pass-only` with commit and
visible-device provenance in its record.

The stage 8 pass of that trial added: a teacher-forced natural-text reference and a repeated serial
baseline in the datatype sweep, the accuracy bar applied per reference with the binomial margin of a
100-token score, the resumed-prefill check rerun on the selected policy with a per-position HF
comparison to separate precision loss from a resume bug, the per-group scope of the BFP4 fidelity
comparison and kernels that ignore a fidelity field, the mid-stage timing-regime split and chain-driver
stop rule in device usage, the Claude Code form of `TT_AUTODEBUG_ROOT`, the DRAM-sharded reader limit,
and `--build-kwargs` / `--build-kwargs-file` on the four generator-level readiness runners
(`readiness_check/build_kwargs.py`, authored in this plugin).

The stage 9 pass of that trial added: the revision-dependent plugin registration mechanisms with
verification in every engine subprocess, the hand-run sampling suite on a plugin layout the runner
rejects, warmup over the served KV cache because paged-attention program keys include its shape,
offset-independent continuation programs and the context-end bound as Stage 9 requirements, the
`--async-scheduling` flag and the device-mirrored position rule with a served greedy-determinism check,
the `tt_supported_decode_batch_sizes` key, the bounded kill of an orphaned engine subprocess, a decode
replay at the end of every tracked workload, the admission-pattern evidence for seeded mixed-batch
sampling failures, and a same-length generator control for the served qualitative verdict.
`check_serving_contract.py` reports prose in an `evidence` field as advisory instead of failing on a
path longer than the filesystem allows.

The stage 10 pass of that trial added: the kernel-family condition on batched-equals-solo bit
equality (`minimal_matmul` is its own family), the three op facts and the scratch-block contract
behind the equal-padded-length design, calibration after the kernel-family choice, row pieces
through the batch dimension for wide batched outputs, one counted sampling pass before a
penalties-on capture, seeded requests that change sampler mid-request as routing defects, the
watcher probe over every prepared batched shape, commit hooks before the first device run of a
commit, and `--output-dir` on the vLLM runner (`run_vllm_server.py`) so a tracked or A/B run does
not overwrite the gate's `readiness_vllm/` artifacts.

## Stack freshness at startup (0.1.22)

The skills told agents to resolve current dependency versions for a new run but enforced nothing:
a bringup could start from a month-old tt-metal branch, a TT vLLM plugin checkout 65 commits
behind its default branch, a `transformers` release the checkpoint config rejects, or a stale
installed copy of this plugin, and no gate noticed. `scripts/check_stack_freshness.py` is authored
in this plugin. In `resolve` mode it fetches the tt-metal and plugin default branches, reads the
checkpoint's `transformers_version` and the published plugin version, writes `doc/stack_pins.json`,
and is critical for a new run on any stale part (advisory on `--resume`). In `verify` mode the new
stage 1 gate and the stage 9 gate confirm the record exists, the checkout still descends from its
pin, and the serving contract's plugin commit equals the pinned one. The model-bringup skill's
"Stack freshness" section states the rule; `serving-stack.md` moves the plugin pin to startup.

Only packaged hashes for one edited stage prompt and one edited check script change in
`sync-source.json`; the original import provenance stays intact.
