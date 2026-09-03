# Tenstorrent skills

The Tenstorrent plugin marketplace for agents working on tt-metal, TTNN, Metalium, models, and
related projects. Register one repository, then choose only the focused plugins your task needs.

The small `tt-skills` plugin contains `tt-skills-finder`. It recommends relevant optional plugins
but does not install, enable, or invoke them without the user's action or explicit permission.

## Install the finder

Codex CLI:

```bash
codex plugin marketplace add git@github.com:tenstorrent/skills.git
codex plugin add tt-skills@tenstorrent-skills
```

The first command registers the marketplace. The second command installs only the finder. Other
plugins remain available for the user to select individually in the **Tenstorrent Skills** section
of the Plugins Directory.

Claude Code:

```text
/plugin marketplace add git@github.com:tenstorrent/skills.git
/plugin install tt-skills@tenstorrent-skills
```

Adding the Claude marketplace installs nothing by itself. The second command installs only the
finder; it can then recommend an optional plugin such as `tt-review-skills`.

Both marketplace commands use your existing GitHub SSH access.

> [!IMPORTANT]
> **Internal note:** This repository is currently private. When it is made public, change both
> marketplace-add commands to the simpler `tenstorrent/skills` form.

## Plugin catalogue

| Plugin | Installation | Purpose |
|---|---|---|
| `tt-skills` | Explicit after marketplace registration | Recommends relevant Tenstorrent plugins while preserving user choice |
| `tt-review-skills` | Optional | Domain-aware PR and diff review for TTNN, Metalium, LLK, model, serving, multi-chip, trace, precision, testing, and L1 changes |
| `tt-autodebug` | Optional | Inspection-only debugging for code issues and hangs, followed by tenacious experiments that find and address the root cause |

Install AutoDebug only when you want its debugging workflow:

```bash
codex plugin add tt-autodebug@tenstorrent-skills
```

For Claude Code, run `/plugin install tt-autodebug@tenstorrent-skills`. Once installed, its
`autodebug`, `autotriage`, and `autofix` skills can be selected automatically as the task
requires. The AutoDebug skill launches a fresh inspection-only agent process to keep deep
investigation out of the calling agent's context.

Prompt development and backtesting currently continue in a maintainer-local standalone repository.
The published plugin is a self-contained snapshot; see
[`plugins/tt-autodebug/SYNC.md`](plugins/tt-autodebug/SYNC.md) for the manual synchronization
contract.

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

## Credit

**This repo is an aggregation. Almost none of the knowledge in it is ours.** The skills here are
vendored, reshaped and re-framed from work other people did — often work that took years of
debugging to learn. The structure is borrowed too.

Everything below was consulted while building this, whether or not content was ultimately taken.
Primary author is the top contributor to that path by commit count; see [`SOURCES.md`](SOURCES.md)
for the full per-skill list, which credits **every** contributor to each path, not just the primary
one.

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
| [`tt-metal`](https://github.com/tenstorrent/tt-metal) — `.github/bug_checker` | [@stevendae](https://github.com/stevendae) | Rules distilled from ~1,398 merged fix PRs: program-cache correctness, op validation, CCL ring buffers, stale LLK config. Strong evidence of which failures *recur*; see [`SOURCES.md`](SOURCES.md) for four of its technical claims we corrected |
| [`tt-metal`](https://github.com/tenstorrent/tt-metal) — `tech_reports/Handling_Special_Value` | [@ttmtrajkovic](https://github.com/ttmtrajkovic) | NaN/Inf/denormal semantics and the FPU/SFPU divergence |

### Structure and tooling

| Source | Primary author | What came from it |
|---|---|---|
| [`mattpocock/skills`](https://github.com/mattpocock/skills) | [@mattpocock](https://github.com/mattpocock) | **The shape of the review catalogue.** Bucketed `skills/<bucket>/<name>/`, progressive disclosure, trigger-style descriptions, `in-progress/` and `deprecated/`, invocation bifurcation, changesets, the install-block convention |
| [`githubnext/gh-aw`](https://github.com/githubnext/gh-aw) | [@dsyme](https://github.com/dsyme), [@pelikhan](https://github.com/pelikhan), [@mnkiefer](https://github.com/mnkiefer) | The consumer. `skills:` frontmatter, `safe-outputs`, and the `mattpocock-skills-reviewer` triage pattern the reference workflow follows |

### Consulted, little or nothing taken

| Source | Primary author | Outcome |
|---|---|---|
| [`tenstorrent/tt-ai-workflow`](https://github.com/tenstorrent/tt-ai-workflow) — `examples/kernel_gen` | [@rlesliehurdTT](https://github.com/rlesliehurdTT) | Reviewed in full. Its special-values documentation pointed us at the public tt-metal tech report, which we used instead. The generation pipeline, templates, and API reference are about *producing* kernels rather than reviewing them, so they were left alone |

**If your work is here and the attribution is wrong, thin, or you would rather it were not — open an
issue and we will fix or remove it.** Everything vendored is Apache-2.0, but licence compliance and
proper credit are different things, and we care about the second one.

## Provenance and drift

Every skill records its upstreams in `metadata.upstream`; [`SOURCES.md`](SOURCES.md) is generated
from that.

Vendored copies rot as upstreams move. `tt-skills-upstream-audit` checks for that, and
[`skills/CLAUDE.md`](skills/CLAUDE.md) carries the review-catalogue invariants for maintainers —
including the **disclosure gate that applies to every re-vendor**. Those rules are deliberately
scoped to `tt-review-skills`; they do not constrain unrelated plugins.

## Validate changes

```bash
python3 scripts/sync_review_plugin.py --check
pytest tests/
claude plugin validate . --strict
```

CI runs the deterministic package-sync check and the Python test suite. The Claude validator is an
additional local check when the CLI is available; Codex plugin manifests are covered by the test
suite and the Codex plugin validator during authoring.

## Licence

Apache-2.0, as are all four upstream sources.
