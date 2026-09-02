# Category 5 — work distribution across cores

`split_work_to_cores` divides work into two groups: `group_1` gets `ceil(total / num_cores)` units,
`group_2` gets `floor(total / num_cores)` — **possibly zero**.

## 5.1 Zero-work cores

If the floor division yields 0, some cores get no work. They still execute the kernel: the reader
fetches nothing, and compute blocks on `cb_wait_front` forever.

Check: can `units_per_core_group_2` be 0 for any valid input shape? If so, is it handled — an early
return (`if (num_tiles == 0) return;`), exclusion of those cores from the launch, or runtime args
that make them skip every loop?

**Failure:** hang. Note that this is shape-dependent, so it commonly escapes review and CI and shows
up on an unusual shape later. Worth flagging even when the tested shapes divide evenly.

## 5.2 Reader, writer and compute must agree

All three kernels on a core must process the **same number of tiles**. If the reader reads N and
compute expects M, the CB either overflows or starves.

Check: in the program descriptor, are the runtime args for all three derived from the same per-core
tile count? Is a transformation applied to one but not the others — writer gets
`num_tiles / reduction_factor` while reader gets `num_tiles`? That is valid only if compute consumes
the difference; verify it does.

**Failure:** CB deadlock, by starvation or overflow.

## 5.3 Runtime args per core group

The descriptor must set different runtime args for `group_1` and `group_2` cores. Using a single
tile count for all cores is a common bug.

Check: does the loop test membership — `core_group_1.contains(core)` / `core_group_2.contains(core)`
or equivalent — and set different counts per group?

**Failure:** all cores given `group_1`'s count → `group_2` cores read past their allocated data →
DRAM corruption or garbage. All cores given `group_2`'s count → `group_1` cores skip tiles →
incomplete output.

Note the asymmetry: the first case corrupts memory belonging to *other* tensors, so the symptom can
surface far from the op that caused it.

## 5.4 CoreRange bounds are inclusive

`CoreRange` coordinates are inclusive-inclusive. `CoreRange({0, 0}, {N, 0})` creates **N+1** cores,
not N.

Check: are end coordinates computed as `count - 1` for 0-indexed grids? Is there an off-by-one
producing one more core than the work distribution accounts for?

**Failure:** the extra core reads out-of-bounds DRAM and corrupts adjacent tensor buffers. This has
caused production bugs presenting as non-deterministic hangs in *downstream* operations — the
failure appears nowhere near the off-by-one.

## Evidence

Cite the descriptor line computing the split and the kernel line consuming the count. For a
zero-work finding, name the input shape that produces the empty group — a concrete shape is what
makes it actionable, and constructing one is usually a two-line calculation.
