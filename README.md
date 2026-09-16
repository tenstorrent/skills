# Tenstorrent skills

Skills for agents working on tt-metal, TTNN, Metalium, models, and related projects.
Tell your agent what you want to do; the finder helps it choose and install the right plugin.
A plugin is a collection of related skills that you can enable or disable together.

**[Getting started with Agentic Research skills](docs/agentic-research/getting-started.md)** —
use AutoDebug, AutoFix, and model bring-up.

## Get started

Ask your Codex or Claude Code agent:

```text
Read https://github.com/tenstorrent/skills and set up the marketplace and
its tt-skills finder plugin for this agent.
```

Once the finder is active, **just ask for the skill you want to use**:

```text
Use AutoDebug to fix <problem>.
```

This is the normal way to use this repository. If the required plugin is missing, the finder
recommends it and asks for your approval. Approve the installation and your agent follows the
host's installation flow, then uses the skills to do the work. Restart the session if prompted.
For this example, it installs `tt-autodebug`, uses AutoDebug to investigate, and AutoFix to repair.

You can also ask:

```text
Use tt-model-bringup on <HF model ID>.
```

Or describe the job, such as “Review this TTNN pull request,” and let the finder recommend a
suitable plugin. You choose which plugins to install. Once installed, their skills can be selected
automatically for matching tasks; you can also ask your agent to make them explicit-only.

## Plugin catalogue

| Plugin | Purpose |
|---|---|
| `tt-skills` | The finder: recommends relevant plugins and helps you install them with your approval |
| `tt-review-skills` | PR and diff review for TTNN, Metalium, LLK, models, serving, multi-chip, trace, precision, testing, and L1 changes |
| `tt-autodebug` | AutoDebug and AutoTriage investigate code issues and hangs; AutoFix tests hypotheses and repairs the cause |
| `tt-model-bringup` | Eleven stages from HF decoder to TTNN/vLLM release, with chunked-prefill guidance, prefill/TTFT optimization, targeted path/serving checks, and published evaluation references with comparability caveats. Requires `tt-autodebug`. |

AutoDebug investigates in a fresh agent process and writes `AUTODEBUG.md`. AutoFix handles source
changes and validation. For examples and expected outputs, see the
[Agentic Research guide](docs/agentic-research/getting-started.md).

If AutoDebug’s Codex child sandbox fails, the calling agent assesses the error and existing
authorization before explicitly retrying with `AUTODEBUG_SKIP_CHILD_SANDBOX=1`; see the
[sandbox guidance](plugins/tt-autodebug/skills/autodebug/SKILL.md#codex-sandbox-startup).

## Model bring-up

Ask your agent to use `tt-model-bringup` with the HF model ID in your target checkout. The finder
recommends both `tt-model-bringup` and its `tt-autodebug` dependency if needed, and asks for approval
to install them.

The stage skills support Codex and Claude Code; automated multi-goal execution uses Codex.
See the [Agentic Research guide](docs/agentic-research/getting-started.md#model-bring-up) for how to
run a bring-up, and [startup and stage orchestration](plugins/tt-model-bringup/skills/model-bringup/SKILL.md)
for detailed setup, dry-run, and resume instructions.

## Alternative manual skill installation

If you prefer to install plugins yourself, use the commands below. GitHub authentication is
required while the repository has restricted visibility.

### Codex

In a terminal, register the marketplace and install the finder:

```bash
codex plugin marketplace add https://github.com/tenstorrent/skills.git
codex plugin add tt-skills@tenstorrent-skills
```

Choose any optional plugins you want:

```bash
codex plugin add tt-autodebug@tenstorrent-skills
codex plugin add tt-review-skills@tenstorrent-skills
# Model bring-up also requires tt-autodebug:
codex plugin add tt-model-bringup@tenstorrent-skills
```

You can also select plugins under **Tenstorrent Skills** in the Plugins Directory.

### Claude Code

Inside the session, register the marketplace and install the finder:

```text
/plugin marketplace add https://github.com/tenstorrent/skills.git
/plugin install tt-skills@tenstorrent-skills
```

Choose any optional plugins you want:

```text
/plugin install tt-autodebug@tenstorrent-skills
/plugin install tt-review-skills@tenstorrent-skills
```

For model bring-up, install `tt-autodebug` above and then:

```text
/plugin install tt-model-bringup@tenstorrent-skills
```

Adding the marketplace does not bulk-install the optional plugins. Codex marks the finder as
installed by default; Claude requires the explicit finder installation shown above.

## Direct gh-aw use

The review skills remain directly pin-able by name in a
[gh-aw](https://github.com/githubnext/gh-aw) workflow:

```yaml
skills:
  - tenstorrent/skills/tt-review-core@<sha>
  - tenstorrent/skills/ttnn-op-kernel-review@<sha>
  - tenstorrent/skills/tt-l1-memory-review@<sha>
```

Pins resolve by skill **name**, not path — the bucket a skill lives in is invisible to the
resolver, so skills can move between buckets without breaking a pin. Always pin a 40-character SHA:
a pin that fails to resolve is reported as a non-fatal warning, so a typo degrades the review
silently rather than failing the run.

See `.github/workflows/tt-pr-review.md` for a complete worked workflow.

## How `tt-review-skills` composes

Load `tt-review-core` first — it carries the severity vocabulary, the evidence rule, the scope
rules, and the do-not-flag guards that every other skill assumes and does not restate. Then load
**at most two** domain skills. A reviewer holding fourteen checklists applies all of them shallowly.

`tt-review-router` maps changed paths to the right subset.

## Reference

### common — cross-cutting review discipline

| Skill | Reviews |
|---|---|
| `tt-review-core` | The shared contract: severity, evidence, scope, output shape, false-positive guards |
| `tt-review-router` | Maps changed paths to the domain skills that apply |
| `tt-test-coverage-review` | PCC bars, tile-boundary cases, program-cache tests, regression tests on bug fixes |
| `tt-perf-claim-review` | Whether a stated performance number is supported by its measurement |
| `tt-comment-hygiene-review` | Iteration-journey comments, tribal knowledge, magic values, op docstrings |
| `tt-split-pr-by-codeowners` | Whether a PR should be split so each piece needs fewer approvals |

### models — model bringup, TTNN consumers

| Skill | Reviews |
|---|---|
| `tt-model-bringup-review` | Residual contract, QKV topology, logical batch vs tile padding, hidden host fallbacks |
| `tt-multichip-ccl-review` | `num_links` vs topology, bias before all-reduce, distributed RMSNorm, gather axes |
| `tt-trace-review` | Capture safety, program-cache warmup signatures, device-owned autoregressive state |
| `tt-precision-review` | Per-tensor-group dtype policy, the prefill/decode cache asymmetry, PCC-collapse triage |

### ttnn — TTNN op authors

| Skill | Reviews |
|---|---|
| `ttnn-op-kernel-review` | The eight structural categories: init, TRISC sync, `tile_regs`, CB UB, work split, semaphores, control flow, in-place |

### metal — tt-metal host and kernel infrastructure

| Skill | Reviews |
|---|---|
| `tt-l1-memory-review` | Buffer inventory discipline, data-movement tiers, CB sizing, accumulator capacity |

### llk — low-level kernels

| Skill | Reviews |
|---|---|
| `llk-race-audit-review` | Nine race hazard classes and the cross-class seams, under a monotonic join contract |
| `llk-perf-audit-review` | Static Tensix perf under a provenance lens and a semantic-equivalence gate |

### inference — serving

| Skill | Reviews |
|---|---|
| `tt-vllm-serving-review` | Generator contracts, plugin registration, the `tt_data_parallel` ambiguity |

### meta — catalogue maintenance

| Skill | Reviews |
|---|---|
| `tt-skills-upstream-audit` | Drift between vendored skills and their upstream sources *(user-invoked)* |

## Attributions

**This repo is an aggregation. ** The skills here are
vendored, reshaped and re-framed from work other people did — often work that took years of
debugging to learn. The structure is borrowed too.

Everything below was consulted while building this, whether or not content was ultimately taken.
Primary author is the top contributor to that path by commit count.

### Content sources

| Source | Primary author | What came from it |
|---|---|---|
| [`tenstorrent/tt-buddy`](https://github.com/tenstorrent/tt-buddy) — `skills/` | [@ppetrovicTT](https://github.com/ppetrovicTT) | The reviewer contract: evidence rule, severity taxonomy, read-past-the-diff discipline, the reviewer cast |
| [`tenstorrent/tt-buddy`](https://github.com/tenstorrent/tt-buddy) — `knowledge/` | [@viktorpusTT](https://github.com/viktorpusTT) | CCL and matmul knowledge, vLLM recipes |
| [`tenstorrent/tt_ops_code_gen`](https://github.com/tenstorrent/tt_ops_code_gen) | [@mstaletovicTT](https://github.com/mstaletovicTT) | The eight-category structural kernel checklist, L1 footprint discipline, memory and precision references |
| [`tt-metal`](https://github.com/tenstorrent/tt-metal) — `.agents` | [@yieldthought](https://github.com/yieldthought) | Optimization rules, multichip, tracing, datatype sweep, vLLM integration — and the Codex PR-review skill this catalogue's output format came from |
| [`tt-metal`](https://github.com/tenstorrent/tt-metal) — `tt-llk/.claude` | [@ndivnicTT](https://github.com/ndivnicTT) | The LLK audit suite as a whole |
| ⤷ `race-audit-all` | [@amahmudTT](https://github.com/amahmudTT) | Nine hazard classes, the monotonic JOIN contract, per-architecture divergence |
| ⤷ `perf-optimization-audit` | [@fvranicTT](https://github.com/fvranicTT) | The provenance lens, semantic-equivalence gate, SIMD false-positive guards |
| [`tt-metal`](https://github.com/tenstorrent/tt-metal) — `.github/bug_checker` | [@stevendae](https://github.com/stevendae) | Rules distilled from ~1,398 merged fix PRs: program-cache correctness, op validation, CCL ring buffers, stale LLK config. Strong evidence of which failures *recur* |
| [`tt-metal`](https://github.com/tenstorrent/tt-metal) — `tech_reports/Handling_Special_Value` | [@ttmtrajkovic](https://github.com/ttmtrajkovic) | NaN/Inf/denormal semantics and the FPU/SFPU divergence |

### Structure and tooling

| Source | Primary author | What came from it |
|---|---|---|
| [`mattpocock/skills`](https://github.com/mattpocock/skills) | [@mattpocock](https://github.com/mattpocock) | **The shape of the review catalogue.** Bucketed `skills/<bucket>/<name>/`, progressive disclosure, trigger-style descriptions, `in-progress/` and `deprecated/`, invocation bifurcation, changesets, the install-block convention |
| [`githubnext/gh-aw`](https://github.com/githubnext/gh-aw) | [@dsyme](https://github.com/dsyme), [@pelikhan](https://github.com/pelikhan), [@mnkiefer](https://github.com/mnkiefer) | The consumer. `skills:` frontmatter, `safe-outputs`, and the `mattpocock-skills-reviewer` triage pattern the reference workflow follows |


## Validate changes

```bash
python3 scripts/sync_review_plugin.py --check
pytest tests/
claude plugin validate . --strict
```

CI runs the deterministic package-sync check and the Python test suite. The Claude validator is an
additional local check when the CLI is available; Codex plugin manifests are covered by the test
suite and the Codex plugin validator during authoring.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the plugin layout, contribution workflow,
versioning, validation, and attribution requirements. Maintainers are listed in
[CODEOWNERS](.github/CODEOWNERS).

## Security

For reporting security vulnerabilities privately, see [SECURITY.md](SECURITY.md).

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).

## License

Licensed under the [Apache License, Version 2.0](LICENSE), with third-party notices
and license details in [NOTICE](NOTICE). See [LICENSE_understanding.txt](LICENSE_understanding.txt)
for the accompanying Tenstorrent rights clarification.
