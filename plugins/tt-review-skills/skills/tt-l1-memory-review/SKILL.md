---
name: tt-l1-memory-review
description: Reviews per-core L1 footprint and circular-buffer sizing — buffer inventory discipline, data-movement cost across memory tiers, CB capacity versus tile counts, and accumulator sizing. Use when reviewing program factories, CB allocation, blocking or work-split changes, or any change that adds a buffer.
metadata:
  tier: kernel
  upstream:
    - repo: tenstorrent/tt_ops_code_gen
      ref: e9c9417eee23c6783b5e72d6a2eed9f75f389fc4
      path: references/l1-footprint-discipline.md
    - repo: tenstorrent/tt_ops_code_gen
      ref: e9c9417eee23c6783b5e72d6a2eed9f75f389fc4
      path: skills/memory-budget-metal/SKILL.md
    - repo: tenstorrent/tt_ops_code_gen
      ref: e9c9417eee23c6783b5e72d6a2eed9f75f389fc4
      path: references/ttnn-cb-memory-fundamentals.md
---

# L1 footprint and CB sizing review

Assumes `tt-review-core`. Reviews how much L1 an op uses, why it uses that much, and whether the
CBs are sized consistently with how the kernels actually use them.

## The failure this exists to catch

L1 pressure gets absorbed as *"solve for a smaller block"* instead of *"need fewer buffers"*. Once
an op has a budget predicate and a blocking search, every new pressure is answered by turning a
knob, and the buffer inventory is never revisited. The result is an op elaborate about **fitting**
its footprint and silent about **why the footprint is what it is** — carrying scratch buffers
nobody justified, in a solve that prices them forever.

Minimising L1 and shrinking the inventory are different activities. An op can be sophisticated at
the first while never attempting the second. **When a diff adds a knob, a heuristic, or a safety
fraction to make an op fit, the finding is about the inventory, not the knob.**

## What to check

**Inventory before solve.** Does the change add a buffer? Is there a stated reason that buffer must
exist, distinct from "the solve accommodates it"? A buffer that should not exist cannot be solved
away — a solve built around it makes it permanent and hard to see.

**One decision per buffer.** Each CB should have a legible justification: what it holds, whose
lifetime it spans, why an existing buffer could not serve. Reviewing a new CB with no such rationale
is a `SHOULD-FIX` even when it fits.

**Disjoint lifetimes.** Two buffers alive at different times can share. Does the change introduce a
buffer whose lifetime is disjoint from an existing one, without reusing it?

**CB capacity against use.** Cross-check `num_pages` in the descriptor against every tile count in
`cb_*` calls. Non-dividing counts are UB — see `ttnn-op-kernel-review` category 4, which owns that
finding. Here the concern is *sizing*: is the accumulator CB large enough for the spill it must
hold? An undersized accumulator blocks on `cb_reserve_back` and deadlocks.

**Tile alignment including padding.** Sizing computed from logical shape rather than padded tile
count under-allocates. Check that the padded count is what reaches the CB configuration.

**Work-split as a memory decision.** See `references/data-movement-tiers.md`. Splitting an axis
changes each core's slice size, so it changes what stays resident and how many times an input
crosses the most expensive tier. A split that fills the grid but re-reads an input from DRAM can
lose to one that fills the grid and does not.

## Severity calibration

- Undersized buffer that can deadlock → `MUST-FIX`.
- Unjustified buffer that fits → `SHOULD-FIX`. It is a real cost: it constrains every future shape.
- A knob added instead of an inventory review → `SHOULD-FIX`, and say which buffer to re-justify.
- Suboptimal split with no measurement either way → `CONSIDER`. The trade is a hypothesis until
  measured; do not assert a winner from the diff alone.

## References

| File | Read when |
|---|---|
| `references/data-movement-tiers.md` | The change alters work split, blocking, or core count |
| `references/reduce-accumulate-constraints.md` | The change touches `reduce` with `Accumulate` |
