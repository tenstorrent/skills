---
name: tt-precision-review
description: Reviews dtype and math-fidelity policy — per-tensor-group precision, the prefill versus decode KV-cache dtype asymmetry, reduced-cache candidates, and PCC-collapse triage. Use when reviewing changes to dtypes, math fidelity, compute kernel configs, or cache precision.
metadata:
  tier: model
  upstream:
    - repo: tenstorrent/tt-metal
      ref: d58cb341c703310cf41b5d88baafc0790ec0270b
      path: .agents/skills/datatype-sweep/SKILL.md
      branch: agentic-research/fast-models-fast
    - repo: tenstorrent/tt_ops_code_gen
      ref: e9c9417eee23c6783b5e72d6a2eed9f75f389fc4
      path: references/precision_convention.md
    - repo: tenstorrent/tt_ops_code_gen
      ref: e9c9417eee23c6783b5e72d6a2eed9f75f389fc4
      path: skills/numeric-formats-metal/SKILL.md
    - repo: tenstorrent/tt-metal
      ref: ce91f33c0c7184618d60553e4b32910c5ebdbfaa
      path: tech_reports/Handling_Special_Value/special_values.md
---

# Precision and fidelity review

Assumes `tt-review-core`. Reviews dtype and math-fidelity decisions, which are policy per tensor
group rather than a single global setting.

## Precision is per tensor group

Weights, activations, KV cache, and accumulators have separate policies. A diff that changes "the
dtype" without saying which group is under-specified — ask which, because the answer changes whether
the change is safe.

Check that a stated policy actually **reached the measured ops**. A policy declared in config and
silently overridden by an op default is a common and invisible failure: the config says one thing,
the op report says another, and nobody looks at both. If a diff claims a precision change, the
evidence is the op-level report, not the config diff.

## The prefill / decode cache asymmetry

This is the trap worth knowing in detail, because the naive fix is wrong in a specific way.

A reduced KV-cache dtype — typically BFP8 / `bfloat8_b` — is usually the right policy when SDPA or
FlashDecode is a material cost. **A functional baseline using BF16 is not evidence that BF16 is the
optimised policy**; it is evidence that nobody revisited it.

When a reduced cache fails, the fix is in the fill and update contract, not a revert:

- **Prefill** fill-cache tensors may need an **explicit cast to the cache dtype**.
- **Decode** `paged_update_cache` inputs may need to stay **BF16 or FLOAT32**.

Those two pull in opposite directions, which is why "just cast everything to the cache dtype" fails
and gets read as "BFP8 does not work here." Flag a BF16 fallback that has no stated blocker — an
acceptable fallback names a correctness failure, an op-contract limit, or a measured same-context
win that includes the SDPA and cache cost.

## Special values

Tensix is **not fully IEEE compliant** for NaN and Inf, and the FPU and SFPU differ from each other
— `0 x Inf` and `Inf - Inf` are defined NaN on the SFPU and unspecified on the FPU. Ops outside
those tables treat specials as ordinary numbers, so "IEEE says so" is not an argument about a TT
kernel in either direction. Denormals flush to zero.

Read `references/special-values.md` when a diff fuses, decomposes, or moves an expression between
engines, or when a PCC collapse might be special-value poisoning rather than precision loss.

## PCC collapse means a bug, not a precision limit

A large PCC drop from a precision change is a **bug signal**, not evidence that the precision is too
low. Reduced precision degrades gracefully; it does not collapse. When PCC falls off a cliff, look
for a layout or cast contract broken by the change — a missing cast, a wrong cache layout, a head
mapping that changed with packing — before concluding the dtype is unusable.

Treat "we tried BFP8 and PCC collapsed, so we kept BF16" as an **unfinished investigation**, and say
so. This is the highest-value finding in this skill, because the conclusion looks reasonable and
usually is not.

## Synthetic weights are not a blocker

A PCC failure under random or synthetic weights, where real target-model weights pass, is **not** an
exact blocker. Reconcile the discrepancy against real-weight coverage before accepting a
higher-precision fallback justified only by a synthetic stress case.

Symmetrically: a synthetic pass does not license a precision reduction that real weights have not
been tested against.

## Fidelity is not dtype

Math fidelity and storage dtype are separate knobs and are frequently conflated in review. A change
to one is not a change to the other, and a sweep that moves both at once cannot attribute its
result. If a diff changes both, ask for them separately or treat the attribution as unestablished.

## Severity

A broken cast contract producing wrong numbers is `MUST-FIX`. An unexplained BF16 fallback, or a
precision policy that did not reach the measured ops, is `SHOULD-FIX`. An untried reduced-cache
candidate where SDPA is material is `SHOULD-FIX` — name the candidate. A dtype choice that is merely
conservative is `CONSIDER`.
