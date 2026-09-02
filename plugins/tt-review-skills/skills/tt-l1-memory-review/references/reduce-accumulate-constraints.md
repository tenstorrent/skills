# Reduce with Accumulate — constraints and sizing

Constraints on the reduce helper's `Accumulate` path, and the CB sizing they imply.

## Accepted combinations

**`AccumulateViaAdd` constraints:** SUM, or standalone AVG. Float only — `Int32` must use
`ReduceTile`. With `Accumulate`, SUM only and `BulkWaitBulkPop` only. Streaming
(`WaitAndPopPerTile`) is contiguous-only, so `REDUCE_COL` is out.

**`ReduceWithinTile::Skip`** elides the within-tile collapse for inputs already collapsed on that
axis — per-core partials that each came out of an earlier `REDUCE_ROW` and are therefore
column-0-valid. Requires `AccumulateViaAdd` explicitly (`Auto` + `Skip` does not compile), and SUM.

**`Accumulate` rejects** MAX + `REDUCE_SCALAR` on all architectures — the running max cannot be
reproduced by the reload. It also rejects MAX + `REDUCE_ROW` **on Quasar only**, where the reload
needs a within-face transpose that `copy_tile_to_dst_init_short` asserts against. On Wormhole and
Blackhole, MAX + `REDUCE_ROW` + `Accumulate` works.

The architecture split matters in review: code that is correct on Wormhole fails to compile on
Quasar here, so a diff adding MAX + `REDUCE_ROW` + `Accumulate` needs checking against the
architectures it claims to support, not just the one it was developed on.

## Last-block routing is the caller's

No wrapper does it. `partial_scaler` (non-tile-aligned reduce dimension: only the last block holds
the last tile) and `post_reduce_op` (a finaliser running once, after the final accumulation) belong
on the **last block only**.

Because the two calls differ in a template parameter, this is an `if`/`else` over two `reduce<>`
calls, not a ternary. Flag a ternary here — it will not compile for the reason the author expects,
and the resulting workaround is usually worse.

For a cross-chunk mean the cleaner idiom is plain `reduce<SUM>` + `Accumulate::at` on non-last
chunks, and `reduce_mean<>` with the **grand-total** `n_reduced` + `Accumulate::at_last` on the
last.

## The scaler trap

`AVG + REDUCE_SCALAR` applies the scaler twice, row then column. The dataflow helper therefore bakes
`1/sqrt(N)`, **not** `1/N`.

This one is worth checking on every touch: the code looks wrong when it is right, so a reviewer
unfamiliar with it "corrects" `1/sqrt(N)` to `1/N` and introduces a numerical bug that PCC catches
only if the test covers that path. If a diff changes a scaler constant, confirm which regime it is
in before flagging either way.

## Sizing consequence

Accumulation spills partial results to an intermediate CB before `tile_regs_release()` and reloads
next iteration. **The accumulator CB must be sized for the spill.** Undersized → the spill blocks on
`cb_reserve_back` → deadlock.

Check the accumulator's `num_pages` in the descriptor against what one iteration spills, and
remember `BulkWaitBulkPop` holds the whole bulk rather than one tile at a time.
