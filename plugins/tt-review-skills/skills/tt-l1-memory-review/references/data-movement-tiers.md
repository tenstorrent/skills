# Aggregate data-movement cost, tiered

Occupancy is necessary, not sufficient. A work split does not only decide how many cores are busy —
it decides **which memory tier the data crosses, and how many times.** Rank candidate splits by
aggregate movement cost, cheapest first:

| Tier | Cost |
|---|---|
| core-local L1, already resident | free — no transfer happens |
| cross-core NoC (gather, multicast, combine) | a transfer, plus a rendezvous |
| DRAM | a transfer, off-chip |

## The ordering is the claim; the regime is not

What *dominates* a tier's cost is set by the shape of the transfers — size, count, fan-out — not by
the tier. Do not assume DRAM is bandwidth-bound and cross-core is latency-bound:

- A **DRAM** access is bandwidth-bound only when large and coalesced. Many small or scattered page
  reads are dominated by per-transaction cost, and no bandwidth argument predicts them.
- **Cross-core** cost splits by pattern. A unicast round trip gated on a semaphore is a latency,
  scaling with fan-in depth. A **multicast of a real payload is neither cheap nor
  latency-dominated** — it moves bytes to every receiver and serialises on the sender, so it scales
  with payload and receiver count. The two behave nothing alike, and a fan-out that wins at one core
  count can lose at another.

So the ranking is by **how many times each tensor crosses each tier**, which is a countable property
of the split. Which tier then dominates is a measurement, not an argument.

## What this means for review

The consequence worth internalising: **a split that fills the grid but re-reads an input from DRAM
can lose to a split that fills the grid and does not**, even when the second adds cross-core
traffic. Splitting an axis is not only a parallelism decision — it changes each core's slice size,
therefore what stays resident, therefore how many times the input crosses the most expensive tier.

When reviewing a split change, ask for the budget: for each tensor, how many times does it cross
DRAM, and what cross-core traffic does the split introduce? That is the number to minimise, subject
to fit — not block coarseness, and not core count.

## Do not assert a winner from the diff

The trade is a hypothesis until measured. If the change alters the split and the author has not
shown a measurement, the finding is *"state the tier-crossing budget for this split"*, not *"this
split is worse"*. Asserting a performance regression you have not measured is exactly the kind of
confident-and-wrong finding that costs a reviewer its credibility.

If a measurement *is* present, `tt-perf-claim-review` owns judging whether it supports the claim.
