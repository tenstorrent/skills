# Sources and attribution

This repo is an **aggregation**. Almost nothing here is original: the skills are vendored,
adapted, and re-shaped from work done by other people in four Tenstorrent repositories. This file
records where each piece came from and who wrote it.

Attribution is the point. Repo names alone are not enough — credit belongs to people, so the table
below lists the **GitHub handles** of everyone who has contributed to each upstream path, most
commits first. Handles rather than display names, so credit points at an account you can follow.

The README carries a shorter, human-facing credit list including sources we consulted but did not
vendor from.

## Upstream repositories

| Source | Visibility | Vendored |
|---|---|---|
| `tenstorrent/tt-buddy` | private | Reviewer cast and the shared reviewer contract; CCL and matmul knowledge |
| `tenstorrent/tt_ops_code_gen` | private | Static-analysis checklist, L1 footprint discipline, memory and precision references |
| `tenstorrent/tt-metal` @ `agentic-research/fast-models-fast` | public | `.agents` skills: optimize, multichip, tracing, datatype-sweep, vllm-integration |
| `tenstorrent/tt-metal` @ `main` | public | `tt_metal/tt-llk/.claude`: race audits and the SFPU perf audit; `.github/bug_checker/rules` |
| Codex skill `tt-metal-pr-review` | — | PR-review checklist, TTNN dealloc and vLLM-DP false-positive guards |

This repository uses [Apache-2.0](LICENSE). See [NOTICE](NOTICE) for third-party
attribution and [LICENSES/](LICENSES/) for the preserved MIT license texts for the
catalogue structure and workflow sources credited in the README. This file
preserves the detailed contributor and source records.

## Private upstream sources

Content from `tt-buddy` and `tt_ops_code_gen` was copied into this collection deliberately, with
approval. Two consequences worth stating plainly:

1. **Naming those repositories here discloses that they exist** and roughly how they are laid out.
   That is a much smaller disclosure than the vendored text itself, and attribution was judged worth
   it.
2. **Vendoring gets a review, with a deliberately narrow scope.** Architecture detail — including
   Quasar — stays: ordering semantics and per-architecture divergence are what make these skills
   worth having, and most of this material is already public in tt-metal. What gets removed is
   internal-only pointers (Confluence page IDs and similar, which are dead links in a public repo
   rather than sensitive), machine-specific paths, and personal identity mappings. The same review
   applies to every re-vendor. See `CLAUDE.md`.

## Reimplemented, not vendored

`skills/common/tt-review-core/scripts/linkify_review.py` is a **reimplementation** from the
documented behaviour of the upstream Codex skill's script. The original lives on its author's
machine and was not available to copy, so the behaviour was reconstructed from its specification
(convert `path:line` and `path#Lline` to commit permalinks, normalise backtick-wrapped refs). It is
not a copy and may differ in edge cases.

## The bug_checker rules are different evidence — about frequency, not mechanism

Most of this catalogue is expert-authored guidance. The `.github/bug_checker/rules` material is
distilled from an audit of roughly 1,398 merged `fix`-labelled tt-metal PRs.

**What that establishes is which failures actually occur, and how often.** "This shipped 74 times" is
real evidence about *what to prioritise* — it is why op-level input validation earned a category of
its own rather than a bullet.

**What it does not establish is that any given rule's technical content is right.** Frequency data
says a class of bug is common; it says nothing about whether the rule's description of the mechanism
is accurate. Those are different claims, and volume of provenance does not transfer from the first
to the second.

**So there is no precedence rule.** An earlier version of this file said that where bug_checker and
expert guidance disagree, bug_checker wins. That was wrong, and four defects entered this repo behind
it — see the corrections below. On a technical conflict, neither source wins by provenance: **read
the code the rule describes.** Where that is not possible, `tt-review-core`'s ground-or-abstain rule
applies to maintainers exactly as it applies to reviewers.

## Corrections to upstream sources

Vendoring is not endorsement, and the `bug_checker` rules were vendored before they had been
reviewed against the code they describe. Four defects were carried in and have since been corrected
here. Found by a GPT-5.6 analysis posted by [@bbradelTT](https://github.com/bbradelTT) on
[tt-metal#54114](https://github.com/tenstorrent/tt-metal/pull/54114), and verified against the tree
before acting.

| Upstream claim | Correction |
|---|---|
| `is_sharded()` guards a `shard_spec().value()` dereference | It does not. `is_sharded()` is true for `ND_SHARDED`, whose `MemoryConfig` sets `shard_spec` to `nullopt` — the spec is in `nd_shard_spec()`. Guard on `has_value()` |
| Math config writes need both math engines drained | Architecture- and field-specific. An FPU-only field needs only the FPU drained; requiring both flags correct code |
| An `_init_` with a non-restoring `_uninit_` is a bug | A no-op teardown is correct where the state is transient and reprogrammed by the next init. In-tree implementations document this |
| `apply_descriptor_runtime_args()` is a rebuild | It applies args from an existing descriptor; against a minimal CB-only descriptor it is a cheap cache-hit repair |

Two further defects in those rules — a non-existent `cb.index` field and a deprecated `tt::stl::hash`
namespace — did not reach this repo, because the code examples containing them were not vendored.

**The drift audit cannot catch this class of error.** It compares *then* against *now*; it has no
opinion on whether *then* was right. Only reading a rule against the code it describes does.

## Adapted, not copied verbatim

Every skill here was reshaped for gh-aw consumption. The main structural changes:

- **Posting removed.** The upstream Codex skill posted review comments through the GitHub API.
  gh-aw agents run read-only and the workflow posts via `safe-outputs`, so all posting steps were
  deleted rather than ported.
- **Machine-specific content scrubbed** — absolute paths, an identity mapping, and harness-specific
  instructions.
- **Progressive disclosure enforced.** Monolithic upstream audits were split into a router plus
  bounded reference files; the pytest suite enforces the bounds.
- **Framing shifted from doing to reviewing.** Several upstreams instruct an agent performing
  optimization work. Here they instruct an agent *reviewing someone else's* work, which changes what
  counts as a finding.

## Per-skill provenance

Generated by `python3 skills/meta/tt-skills-upstream-audit/scripts/check_drift.py --sources`.
Regenerate it after any re-vendor rather than editing by hand.

| Skill | Upstream | Path | Pinned | Contributors (most commits first) |
|---|---|---|---|---|
| `tt-comment-hygiene-review` | `tenstorrent/tt-buddy` | `skills/code-review/reviewers/fresh-eye.md` | `ba9021417442` | ppetrovicTT |
| `tt-comment-hygiene-review` | `tenstorrent/tt-buddy` | `skills/code-review/reviewers/documentation.md` | `ba9021417442` | ppetrovicTT |
| `tt-perf-claim-review` | `tenstorrent/tt-metal` | `.agents/skills/optimize/SKILL.md` | `d58cb341c703` | yieldthought |
| `tt-perf-claim-review` | `tenstorrent/tt_ops_code_gen` | `skills/perf-measure/SKILL.md` | `e9c9417eee23` | dstoiljkovicTT, mstaletovicTT, djordjenTT |
| `tt-review-core` | `tenstorrent/tt-buddy` | `skills/code-review/shared.md` | `ba9021417442` | ppetrovicTT |
| `tt-review-core` | `tenstorrent/tt-buddy` | `skills/code-review/review-loop.md` | `ba9021417442` | ppetrovicTT |
| `tt-review-core` | `tenstorrent/tt-metal` | `.agents/skills/code_quality_review` | `d58cb341c703` | tchedaTT |
| `tt-review-router` | `tenstorrent/tt-buddy` | `skills/buddy/SKILL.md` | `ba9021417442` | ppetrovicTT |
| `tt-review-router` | `tenstorrent/tt-metal` | `tt_metal/tt-llk/.claude/skills/race-audit-all` | `ce91f33c0c71` | amahmudTT |
| `tt-test-coverage-review` | `tenstorrent/tt-buddy` | `skills/code-review/reviewers/qa.md` | `ba9021417442` | ppetrovicTT |
| `tt-test-coverage-review` | `tenstorrent/tt_ops_code_gen` | `skills/golden-tests/SKILL.md` | `e9c9417eee23` | mstaletovicTT, djordjenTT, dstoiljkovicTT |
| `tt-vllm-serving-review` | `tenstorrent/tt-metal` | `.agents/skills/vllm-integration/SKILL.md` | `d58cb341c703` | yieldthought, tchedaTT |
| `tt-vllm-serving-review` | `tenstorrent/tt-buddy` | `knowledge/recipes/vllm` | `ba9021417442` | ppetrovicTT, viktorpusTT |
| `llk-perf-audit-review` | `tenstorrent/tt-metal` | `tt_metal/tt-llk/.claude/skills/perf-optimization-audit` | `ce91f33c0c71` | fvranicTT |
| `llk-perf-audit-review` | `tenstorrent/tt-metal` | `tech_reports/Handling_Special_Value/special_values.md` | `ce91f33c0c71` | blozano-tt, ttmtrajkovic, jasondavies, ndivnicTT |
| `llk-race-audit-review` | `tenstorrent/tt-metal` | `tt_metal/tt-llk/.claude/skills/race-audit-all` | `ce91f33c0c71` | amahmudTT |
| `llk-race-audit-review` | `tenstorrent/tt-metal` | `.github/bug_checker/rules/llk-stale-hw-config-state.md` | `fb5c6cfa6f08` | blozano-tt |
| `tt-l1-memory-review` | `tenstorrent/tt_ops_code_gen` | `references/l1-footprint-discipline.md` | `e9c9417eee23` | mstaletovicTT |
| `tt-l1-memory-review` | `tenstorrent/tt_ops_code_gen` | `skills/memory-budget-metal/SKILL.md` | `e9c9417eee23` | mstaletovicTT, astancovTT, wransom-TT |
| `tt-l1-memory-review` | `tenstorrent/tt_ops_code_gen` | `references/ttnn-cb-memory-fundamentals.md` | `e9c9417eee23` | mstaletovicTT, astancovTT, wransom-TT, dstoiljkovicTT |
| `tt-model-bringup-review` | `tenstorrent/tt-metal` | `.agents/skills/optimize/SKILL.md` | `d58cb341c703` | yieldthought |
| `tt-model-bringup-review` | `tenstorrent/tt-metal` | `.agents/skills/functional-decoder/SKILL.md` | `d58cb341c703` | yieldthought |
| `tt-model-bringup-review` | `tenstorrent/tt-buddy` | `knowledge/matmul.md` | `ba9021417442` | ppetrovicTT |
| `tt-multichip-ccl-review` | `tenstorrent/tt-buddy` | `knowledge/ccl.md` | `ba9021417442` | ppetrovicTT |
| `tt-multichip-ccl-review` | `tenstorrent/tt-metal` | `.agents/skills/multichip/SKILL.md` | `d58cb341c703` | yieldthought |
| `tt-multichip-ccl-review` | `tenstorrent/tt-metal` | `.github/bug_checker/rules/ccl-ring-buffer-mismatch.md` | `ce91f33c0c71` | stevendae |
| `tt-precision-review` | `tenstorrent/tt-metal` | `.agents/skills/datatype-sweep/SKILL.md` | `d58cb341c703` | yieldthought |
| `tt-precision-review` | `tenstorrent/tt_ops_code_gen` | `references/precision_convention.md` | `e9c9417eee23` | djordjenTT |
| `tt-precision-review` | `tenstorrent/tt_ops_code_gen` | `skills/numeric-formats-metal/SKILL.md` | `e9c9417eee23` | mstaletovicTT |
| `tt-precision-review` | `tenstorrent/tt-metal` | `tech_reports/Handling_Special_Value/special_values.md` | `ce91f33c0c71` | blozano-tt, ttmtrajkovic, jasondavies, ndivnicTT |
| `tt-trace-review` | `tenstorrent/tt-metal` | `.agents/skills/tt-enable-tracing/SKILL.md` | `d58cb341c703` | yieldthought, tchedaTT |
| `ttnn-op-kernel-review` | `tenstorrent/tt_ops_code_gen` | `references/static-analysis-checklist.md` | `e9c9417eee23` | mstaletovicTT, astancovTT |
| `ttnn-op-kernel-review` | `tenstorrent/tt_ops_code_gen` | `skills/debug-ttnn-op` | `e9c9417eee23` | mstaletovicTT |
| `ttnn-op-kernel-review` | `tenstorrent/tt-metal` | `.github/bug_checker/rules/reshape-dim-check.md` | `ce91f33c0c71` | stevendae |
| `ttnn-op-kernel-review` | `tenstorrent/tt-metal` | `.github/bug_checker/rules/program-cache-hash-collision.md` | `fb5c6cfa6f08` | blozano-tt |
| `ttnn-op-kernel-review` | `tenstorrent/tt-metal` | `.github/bug_checker/rules/smuggled-buffer-runtime-arg.md` | `fb5c6cfa6f08` | blozano-tt |
| `ttnn-op-kernel-review` | `tenstorrent/tt-metal` | `.github/bug_checker/rules/override-rebuild-in-cache-hit.md` | `fb5c6cfa6f08` | blozano-tt |
| `ttnn-op-kernel-review` | `tenstorrent/tt-metal` | `.github/bug_checker/rules/op-shard-layout-validation.md` | `fb5c6cfa6f08` | blozano-tt |

> **Five rows are provisional.** The `bug_checker` rules from tt-metal PR 54114 are pinned at the
> PR head rather than `main`, because that PR is still open. Re-pin them when it merges —
> `tt-skills-upstream-audit` flags the rows once the branch is deleted.

> `references/special-values.md` is intentionally duplicated in two skill folders. gh-aw copies a
> single skill folder, so a shared reference cannot live in a sibling skill. The test suite asserts
> the copies stay identical.

> If your handle appears here and the credit is wrong, thin, or unwanted — open an issue and we
> will fix or remove it.
