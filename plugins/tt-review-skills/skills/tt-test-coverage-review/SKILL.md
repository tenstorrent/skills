---
name: tt-test-coverage-review
description: Reviews test coverage for Tenstorrent changes — PCC bars and when a lower one is justified, tile-boundary and padding cases, sharding and data-format variants, program-cache tests, and a regression test on every bug fix. Use when a change alters op or kernel behaviour, whether or not it touches tests.
metadata:
  tier: process
  upstream:
    - repo: tenstorrent/tt-buddy
      ref: ba9021417442d59756aa8cdf154a25648c9a0de5
      path: skills/code-review/reviewers/qa.md
    - repo: tenstorrent/tt_ops_code_gen
      ref: e9c9417eee23c6783b5e72d6a2eed9f75f389fc4
      path: skills/golden-tests/SKILL.md
---

# Test coverage review

Assumes `tt-review-core`. Coverage analysis and edge-case hunting: what can go wrong, and whether
anything would catch it.

**This skill applies whenever behaviour changed — not only when `tests/` changed.** A behaviour
change with no test is exactly what it exists to find.

## The bar

**PCC > 0.999 against a PyTorch reference** for every new or modified op or kernel. A lower
threshold needs explicit justification *at the test site*, not in the PR description — the next
person to read the test is the one who needs the reason.

Silently dropping a PCC target is `MUST-FIX`. It is also easy to miss in a diff: look for changed
tolerance constants, not just changed assertions.

## Edge cases that actually bite here

- **Tile boundaries.** Non-multiple-of-32 shapes, and padding. A new op with no tile-edge coverage
  is `MUST-FIX` — this is the single most common gap.
- **Empty and degenerate tensors**, and the zero-work core case from
  `ttnn-op-kernel-review` category 5.1. If a shape can produce an empty work group, a test should
  cover that shape.
- **Data-format matrix.** Is the supported format set actually exercised, or only the default?
- **Sharding variants** — height, width, block, interleaved, as applicable to the op.
- **Multi-device and CCL teardown paths**, where relevant.
- **Architecture gating.** If behaviour is gated per architecture, is it exercised on the
  architectures claimed? A test that silently skips on the arch it was written for is not coverage.

## Program cache and regressions

- **Repeated-invocation tests** verify program-cache hits. A change touching program-cache
  signatures without such a test is `SHOULD-FIX`.
- **Every bug fix needs a test that fails on the pre-fix code.** Absent → `MUST-FIX`. Without it the
  bug returns, and the fix's own diff is the only record of what was wrong.

To judge this, apply the test to the *old* behaviour mentally: if it would have passed before the
fix, it is not a regression test, whatever it is named.

## Registry honesty

Where an op declares `SUPPORTED` / `EXCLUSIONS` / `INVALID`, those declarations are load-bearing —
the eval pipeline consumes them. Two failure shapes:

- A configuration declared supported but not exercised anywhere.
- A configuration excluded to make a suite green, rather than because it is genuinely out of scope.
  The second is a correctness finding wearing a test-hygiene costume. Flag it as such.

`INVALID` means *structural semantic impossibility* — the universe would have to change for the cell
to make sense, such as `bfloat8_b` with `ROW_MAJOR` layout. It is not a place to put "this fails".

## Do not flag

- Trivial code that is obviously correct.
- Unreachable paths.
- Coverage percentage for its own sake. Focus on risk: an untested edge that corrupts memory matters,
  an untested getter does not.
- A deliberately narrow regression test. Ask what the test is *for* before asking it to do more.

## Severity

`MUST-FIX` — untested bug-prone code, missing regression test on a bug fix, silently dropped PCC
target, no tile-edge coverage on a new op. `SHOULD-FIX` — significant functionality, a sharding
variant, or a data format uncovered. `CONSIDER` — additional edge-case scenarios.
