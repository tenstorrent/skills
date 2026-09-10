---
name: tt-review-router
description: Maps changed paths in a Tenstorrent pull request to the domain review skills that apply. Run this first to pick the skill subset for a review.
metadata:
  tier: process
  upstream:
    - repo: tenstorrent/tt-buddy
      license: Apache-2.0
      ref: ba9021417442d59756aa8cdf154a25648c9a0de5
      path: skills/buddy/SKILL.md
    - repo: tenstorrent/tt-metal
      license: Apache-2.0
      ref: ce91f33c0c7184618d60553e4b32910c5ebdbfaa
      path: tt_metal/tt-llk/.claude/skills/race-audit-all
---

# Review router

Triage step. Given a diff, decide which domain skills a reviewer should load. A workflow may call
this explicitly, and an agent reviewing a relevant Tenstorrent change may select it normally.

## Why routing matters

Loading every skill costs context and produces worse reviews, not better ones — a reviewer holding
fourteen checklists applies all of them shallowly. **Load `tt-review-core` plus at most two domain
skills.** If a diff genuinely spans more than two domains, it is usually two reviews.

## Path routing

| Changed path | Load |
|---|---|
| `**/kernels/**`, `**/*_kernel.cpp`, `**/device/**/*_program_factory.*` | `ttnn-op-kernel-review` |
| Program descriptor, CB config, `split_work_to_cores`, blocking or work-split | `tt-l1-memory-review` |
| `ttnn/**/*.py`, `models/**/*.py` (model bringup, layout, memory config) | `tt-model-bringup-review` |
| `tt_metal/tt-llk/**` | `llk-race-audit-review`, and `llk-perf-audit-review` for SFPU or perf changes |
| CCL, fabric, mesh, `all_gather`, `reduce_scatter`, `all_reduce` | `tt-multichip-ccl-review` |
| Trace capture or replay, program cache | `tt-trace-review` |
| Dtype, fidelity, `*_cache_dtype`, precision config | `tt-precision-review` |
| `generator_vllm.py`, vLLM plugin registration, tt-inference-server | `tt-vllm-serving-review` |
| `tests/**` — or any diff whose behaviour change needs a test | `tt-test-coverage-review` |
| A PR body or comment asserting a speedup or a perf number | `tt-perf-claim-review` |
| Any diff (cheap, runs alongside) | `tt-comment-hygiene-review` |

## Routing rules

**Route on what changed, not on where the file lives.** A `.py` file under `models/` that edits a
program config is a memory-config change; a `.cpp` under `ttnn/` that only renames a symbol needs
no domain skill at all.

**Kernel changes almost always pair.** `ttnn-op-kernel-review` and `tt-l1-memory-review` are the
common pair: a new CB is both a structural question and a footprint question.

**Perf claims are separate from perf changes.** A diff that changes blocking routes to
`tt-l1-memory-review`. A PR *asserting* it is 1.4x faster routes to `tt-perf-claim-review`, whatever
the diff touched. Both can apply.

**Tests are not a domain.** `tt-test-coverage-review` applies whenever behaviour changed, not only
when `tests/` changed — a behaviour change with no test is precisely what it exists to catch.

**Nothing matched?** Say so and review with `tt-review-core` alone. An empty domain set is a valid
outcome, and better than forcing an irrelevant skill onto a diff.

## Output

```
## Skills

- tt-review-core          (always)
- <domain skill>          — <the path or change that selected it>
- <domain skill>          — <...>

## Not selected

- <skill> — <why it looked relevant but is not>
```

The `Not selected` section matters when a diff touches a path that *looks* like it should route
somewhere but does not. It stops the next reader re-deriving the same decision.
