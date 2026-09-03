# Category 10a — program cache hashing

Host-side, not kernel-side, but the same failure grade: a wrong cache **hit** produces wrong results
with no error. Distilled from merged tt-metal fixes — see `SOURCES.md`.

The shared premise: on a cache hit the framework reuses a compiled program and re-patches only what
it was told to. Anything the hash omits, or any address the override does not patch, silently
carries over from the first miss.

## Hash omissions

**Every field `create()` reads must be in the hash.** For a new or modified
`compute_program_hash()`, check each attribute and input-tensor field the program factory actually
reads to decide kernel structure. A field read in `create()` but not folded into the hash means two
calls differing only in that field collide.

**Optional inputs and outputs** must hash *provided versus not provided* as a distinct case — not
hash the tensor's shape when present and skip the field entirely when absent. Those two produce the
same hash for "absent" and "present with a shape that hashes to nothing".

**Per-operand broadcast flags.** Where each operand of a binary or ternary op can broadcast
independently, each operand's broadcast decision belongs in the hash. Output shape alone is not
enough — two operand pairs reach the same output shape by different broadcast paths.

**Shape reduced to a scalar.** Two distinct failure modes worth keeping separate:

- **Hashing `shape.rank()`** distinguishes different ranks but *not* same-rank different-dims.
  `[32, 64]`, `[64, 32]` and `[128, 256]` all hash identically — only the number `2` is folded in.
- **Hashing `shape.volume()`** separates element counts but aliases same-volume different-tiling.
  `[32, 64]` and `[64, 32]` are both volume 2048 and tile into different grids.

A custom hash is the risk surface here. The default reflection hash is protected by a canonical-key
comparison, so its failure costs a redundant rebuild rather than a wrong result.

## Severity

A wrong cache hit from a hash omission is `MUST-FIX` — wrong results, no error. It is invisible in a
single-invocation test, so "the unit test passes" carries no weight; ask for the repeated-invocation
test that `tt-test-coverage-review` also requires.

For the runtime-argument and override half of this category, see
`references/program-cache-runtime-args.md`.
