---
name: tt-review-core
description: Shared review contract for all Tenstorrent code review — severity vocabulary, the evidence rule, scope, output shape, and the do-not-flag guards. Use when reviewing any change to a Tenstorrent repository (tt-metal, tt-llk, tt-inference-server, model code), and load it before any domain review skill.
metadata:
  tier: process
  upstream:
    - repo: tenstorrent/tt-buddy
      ref: ba9021417442d59756aa8cdf154a25648c9a0de5
      path: skills/code-review/shared.md
    - repo: tenstorrent/tt-buddy
      ref: ba9021417442d59756aa8cdf154a25648c9a0de5
      path: skills/code-review/review-loop.md
    - repo: tenstorrent/tt-metal
      ref: d58cb341c703310cf41b5d88baafc0790ec0270b
      path: .agents/skills/code_quality_review
      branch: agentic-research/fast-models-fast
---

# Tenstorrent review core

The shared contract. Every domain review skill in this repo assumes these rules and does not
restate them. Load this first; load at most two domain skills on top.

## The evidence rule

**Every finding cites `file:line` plus a source.** A source is one of: a path in the repo under
review, a documented invariant, or a reference file shipped with the skill that raised the finding.

A finding without evidence does not go in the report. This is the single most important rule here —
a domain-loaded reviewer that speculates is worse than no reviewer, because its findings look
authoritative.

If a finding hinges on something you could not verify, say so explicitly in the finding and
**downgrade one severity step** (MUST-FIX → SHOULD-FIX → CONSIDER). Do not suppress it, and do not
state the unverified part as fact.

## Severity

| Label | Meaning |
|---|---|
| `MUST-FIX` | Wrong. Incorrect results, a hang, a race, memory corruption, or a broken contract. |
| `SHOULD-FIX` | Works, but carries real cost: performance left on the table, a maintenance trap, a missing test for changed behaviour. |
| `CONSIDER` | Judgment. A cleaner alternative exists; reasonable people may decline. |

Labels describe **impact, not workflow gates**. Nothing here blocks a merge on its own.

## Scope: read past the diff

A diff is a narrow window. Before flagging anything:

- **Read the whole file**, not just the hunk. Does this addition duplicate something 50 lines up?
- **Grep for the existing pattern** before calling something new. Reinvention is itself a finding.
- **Trace callers and callees.** Signature and semantic changes have consequences off-screen.
- **Find a neighbour** — a sibling ttnn op, a parallel kernel, a comparable test. Structural fit is
  judged against neighbours, not in the abstract.
- **Validate intent.** Does the implementation match what the change is trying to do? Does the test
  actually test that intent? "This works, but it is not what was meant" is a real finding.

## Do not flag these

These are the guards that stop a domain-loaded reviewer from drowning a PR in noise. Each one is a
mistake reviewers actually make on Tenstorrent code.

- **Missing `ttnn.deallocate(...)` is not a leak.** TTNN device buffers are released when the last
  tensor reference is destroyed. Missing explicit deallocation is a *peak memory* question, not a
  leak. Only report retention when references demonstrably outlive their intended scope.
- **"Data parallel" is ambiguous across vLLM and tt-metal.** vLLM's `data_parallel_size` /
  `tt_data_parallel` may mean the SDPA/KV-cache data-parallel degree, while tt-metal code often
  means mesh-local structure (input mesh rows, attention weight copies). Validate the active caller
  contract before flagging any relationship among `tt_data_parallel`, `max_batch_size`,
  `batch_size_per_row`, and mesh dimensions.
- **Do not flag deliberate architecture-specific divergence** as inconsistency without checking
  whether the architectures genuinely differ.
- See `references/false-positive-guards.md` for the full list with worked examples.

## What to flag hardest

- **Slop.** `hasattr` and friends "just to be safe" are not acceptable. Code should be specific
  about what it expects, not permissively accept anything the rest of the repo will never send it.
- **Unnecessary fallback.** Defensive branches that continue past a state that should be impossible
  (a `None` device or mesh). Assert or return an explicit error at the boundary instead.
- **Invariants enforced late.** Preconditions belong at the boundary, not patched downstream.
- **Control flow that obscures the supported cases.**

## Output

Emit findings only. **Never post them.** See `references/output-format.md` for the exact shape.

In a gh-aw workflow the agent runs read-only and the *workflow* posts through `safe-outputs`. A
skill that calls `gh api -X POST` is a bug, not a feature.

## References

| File | Read when |
|---|---|
| `references/false-positive-guards.md` | Before reporting anything that looks like a leak, an inconsistency, or a missing call |
| `references/output-format.md` | Emitting findings |
| `references/scope-and-evidence.md` | A finding depends on context outside the diff, or you cannot verify a claim |
