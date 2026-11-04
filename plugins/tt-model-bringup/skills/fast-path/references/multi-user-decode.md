# Multi-user decode (goal 4)

As soon as a second request arrives, a batch-1 fast path stops applying, and on the reference
model everyone dropped back to the stock decode (3.4 ms → 12.9 ms per step). This goal keeps
several users fast: fused multi-row decode for small widths, then streamed batched decode for
the rest.

## Measure the union first (MoE)

Each row routes to its own experts, so a step with M rows reads the union of their experts.
Dump the router's top-k ids per token and layer from a real-text run and run
`skills/fast-path/scripts/expert_union.py` on them:

```bash
python "$TT_MODEL_BRINGUP_ROOT/skills/fast-path/scripts/expert_union.py" \
  --ids routing_ids.npy --experts 32 --widths 1 2 4 8 --json doc/fast_path/expert_union.json
```

It reports the mean distinct experts per layer for M consecutive tokens of one sequence (the
speculative-check shape) and for M independent users. gpt-oss-20b (32 experts, top-4): consecutive
6.4 / 9.6 / 13.5 at M = 2 / 4 / 8; independent users 7.4 / 12.8 / 19.7. The union, not M, sets the
cost. Dense models read all weights at every width, so their multi-row step costs close to one
row until compute becomes the bound.

## Fused multi-row decode (widths 2-8)

- Read each streamed weight column once for all M rows: M-row activation tiles feeding the same
  stream programs, one shared boundary per layer, per-row norms, per-row LM-head candidates into
  the shared sampler.
- MoE: stream the union of the rows' experts once per step, and give each row its own experts'
  contributions.
- Allocate the multi-row buffers per forward, not persistently. On the reference model width 8
  overflowed L1 on one core (about 650 KB of per-forward buffers), so the shipped widths were 2
  and 4 and width 8 used the batched path. Budget L1 per width before writing kernels.
- Skip inactive rows in the router (position below zero) so padded rows cost nothing.
- Prepare every width before capture; declare them as decode buckets. Keep the stock decode at
  each width captured too, for penalties, log-probs and host sampling.

**Exactness.** A multi-row op that is bit-identical to the batch-1 op row by row is the goal. On
the reference model every op met it except the row norm (PCC 0.999997), so batch-k text differed
from batch-1 text, exactly as the stock batched decode already did. That is acceptable for
multi-user serving (accuracy per width is the gate) and not for speculative decoding, which
needs an exact norm and experts summed in each row's own rank order
([speculative decoding](speculative-decoding.md)).

## The cost fit

Time the width-4 step at forced unions of 4 to 16 experts (a timing-only switch that routes every
row to a chosen set) and fit `step = fixed + slope × union`. On the reference model:
2.82 + 0.359 ms per expert. The slope is bytes: each extra expert read 920,448 bytes per DRAM bank
per layer at 61.5 of 64 GB/s, 96% of peak, so 0.345 ms was its floor and could not move with
identical outputs. The fixed part could: zeroing pad tiles after the first weight reads instead of
streaming them, and splitting the router and the LM-head scan over two cores, cut it to 2.46 ms.
Width 2 went 4.74 → 4.45 ms and width 4 7.08 → 6.72 ms; two users got 355 tok/s in total where
the stock path gave 142.

## Streamed batched decode (widths 8-32)

At 8 to 32 rows nearly every expert is in the union, so stream them all once per step from the
decode copies goal 2 already built (no new weights):

- one-op dense routing that writes the routing columns directly (bit-exact with the old router);
- a fused GLU and a padded ring all-reduce;
- the LM head on the full core grid;
- gate/up weights in an ND-sharded layout matched to the stream readers.

Reference result: width 32 24.25 → 15.56 ms and width 8 18.79 → 12.59 ms; 32 users
1,197 → 1,761 tok/s, 8 users 402 → 606. Rejected with numbers: a replacement router (+0.64 ms),
streaming all experts at widths below 8 (+3.5 ms), DRAM-sharded QKV/output projections (slower
at 32), and a fused matmul + reduce-scatter (hung at 32).

## Gates

Per width: device step before/after (width 1 must not move), accuracy at that width against the
same reference, the served ladder at c = 1, 2, 4, 8, 16, 32 against the previous build (every row
at least 97% of before), and the exactness class of every shared kernel touched (digests).
Write `doc/fast_path/multi_user_decode.json` ([validation](validation.md#goal-4-multi-user-decode)).
