---
name: tt-perf-claim-review
description: Reviews performance claims rather than performance changes — whether the measurement supports the assertion. Covers device duration versus wall time, bound classification, first-run program-cache caveats, absolute versus percentage comparisons, and warmed traced harness parity. Use when a PR body, comment, or test asserts a speedup, a regression, or a throughput number.
metadata:
  tier: process
  upstream:
    - repo: tenstorrent/tt-metal
      ref: d58cb341c703310cf41b5d88baafc0790ec0270b
      path: .agents/skills/optimize/SKILL.md
      branch: agentic-research/fast-models-fast
    - repo: tenstorrent/tt_ops_code_gen
      ref: e9c9417eee23c6783b5e72d6a2eed9f75f389fc4
      path: skills/perf-measure/SKILL.md
---

# Performance claim review

Assumes `tt-review-core`. This skill reviews **claims**, not code. It applies whenever a PR asserts
a number, whatever the diff touched — and it does not apply merely because a diff changes something
performance-relevant. That is `tt-l1-memory-review` and `tt-model-bringup-review`.

## The question

Not "is this faster?" but **"does the measurement presented support the claim made?"** Those come
apart constantly, and almost always in the optimistic direction.

## What a supported claim contains

- **Device duration**, not wall time, for op-level claims. Wall time includes host overhead that the
  change may not have touched.
- **The same harness on both sides.** Same traced warmed path, same context shape, same precision
  policy, same correctness checks. A comparison against a differently-configured baseline is not a
  comparison.
- **Warm, not first-run.** The first invocation includes program-cache compilation. A "speedup" that
  is a warm run against a cold baseline is the single most common bad claim in this domain, and it
  is invisible unless you ask which run produced each number.
- **Absolute figures, not percentages of a shifting total.** Compare absolute throughput or absolute
  duration. A percentage share of total device time falls when *anything else* gets slower, so a
  share improving is not evidence the measured op improved.
- **A bound classification** — is the op bandwidth-bound, compute-bound, or latency-bound? A claim
  that contradicts its own bound needs explaining before it can be accepted.

## Failure shapes to flag

**Op-count reasoning.** "This removes two ops so it must be faster." Op count is not time. Do not
accept it, and do not produce it — the same rule binds the reviewer.

**Local win, global loss.** A projection matmul gets faster while attention or a layout conversion
absorbs the cost. Any claim about a layer needs the layer's total, not one row. A lower total can
justify a local regression only when the regression is named, quantified, and the whole path is
still faster under the same context.

**Unstated variance.** A single run of each side is not a measurement of a small difference. If the
claimed delta is within plausible run-to-run noise and no repetition is reported, say so.

**Directional arguments about non-monotonic knobs.** Some tuning parameters do not improve
monotonically — see `tt-multichip-ccl-review` for measured examples. "Increasing this should help"
is not evidence in either direction.

**Precision changed underneath the comparison.** A geometry or layout sweep that silently changes
dtype or math fidelity is not measuring what it claims. Check that the precision policy is fixed
across the compared candidates.

## What not to do

**Do not assert a counter-claim you have not measured.** If the evidence is insufficient, the finding
is *"this claim is not supported by the measurement shown"* — with the specific gap named. It is not
*"this is actually slower."* You are reviewing a diff, not running the harness, and an invented
counter-number is the same error as the one being flagged.

Concretely: `SHOULD-FIX — the speedup compares a warmed run against a cold baseline; rerun the
baseline warm` is a good finding. `SHOULD-FIX — this is not actually faster` is not.

## Serving-stage exception

vLLM and optimised-vLLM stages deliberately skip Tracy, `tt-perf-report`, and device-profiler
collection. **Do not flag their absence there**, and do not ask for device-level evidence from a
serving-stage change — the collection is skipped on purpose. Serving claims rest on
serving-level metrics instead.

## Severity

An unsupported claim in a PR body is `SHOULD-FIX` — it misleads the next reader and gets cited later
as established. A claim contradicted by evidence in the same PR is `MUST-FIX`. A claim that is
plausible but under-evidenced is `CONSIDER` with the missing piece named.
