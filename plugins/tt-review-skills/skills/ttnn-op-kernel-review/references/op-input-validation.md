# Category 9 — op-level input validation

The eight categories cover correctness *inside* the kernels. This one covers whether the op checked
what it was handed before building a program at all.

`tt-review-core` states the principle — preconditions belong at the boundary, not patched
downstream. This is that principle for TTNN ops, and it is the single largest bug bucket in the
tt-metal fix history. See `SOURCES.md`.

## Shape and volume arithmetic

**Logical volume must be preserved.** Before and after a reshape, `logical_shape.volume()` must
equal `input_tensor.logical_volume()`. Where a dimension is inferred (`-1`), verify the arithmetic
divides exactly.

```cpp
// BUG: volume changes -- 2*3*4 = 24 != 2*3*5 = 30
auto output_shape = ttnn::Shape({2, 3, 5});
auto result = ttnn::reshape(input, output_shape);   // input is {2, 3, 4}
```

**Layout alignment.** For `Layout::ROW_MAJOR`, the last padded dimension must be divisible by the
row-major width alignment, for input and output both. For `Layout::TILE`, padded height and width
must each be tile-aligned and the physical volume must divide by the tile area.

**Padded versus logical shape.** Mixing `padded_shape()` and `logical_shape()` when computing output
dimensions produces wrong results. Padding is layout-specific and applies *after* the logical
reshape — check which one each site uses, because both compile and only one is right.

**Precomputed distribution tables.** Lookup tables sized against core and expert counts must match
the actual counts and the iteration bounds that walk them. A table that was correct when written and
a grid that has since changed is a silent mismatch.

## Layout and memory-config validation

The failure shape: an op reads layout, memory config, or shard spec to build its program **without
validating up front that it supports what it was given**. The result is wrong output or a hang
instead of a clean `TT_FATAL`.

**1. `.shard_spec().value()` without a sufficient guard.** Any dereference of the optional shard
spec not dominated by `shard_spec().has_value()` or an equivalent `TT_FATAL` — including dereferences
inside the validation hook itself, and in helpers the hook calls. The helper case is the one that
gets missed.

> **`is_sharded()` is not a sufficient guard, and treating it as one is itself the bug.** It returns
> true for `ND_SHARDED`, and an ND-sharded `MemoryConfig` is constructed with `shard_spec` set to
> `std::nullopt` — the spec lives in `nd_shard_spec()` instead. So `if (is_sharded()) { …
> shard_spec().value() … }` throws on an ND-sharded tensor. Guard on `has_value()`, or branch on the
> layout and handle ND explicitly.

**2. A validation hook that never mentions layout.** A `validate_on_program_cache_miss()` or
`validate()` that checks dtype, storage type and shapes but never constrains `layout()` or
`memory_config().memory_layout()` — while the program factory branches on, or assumes, one of them.
**If the factory has a TILE-only tile-indexing loop, the validator must require `Layout::TILE`.**

That asymmetry is the tell, and it is mechanically checkable in review: read what the factory
assumes, then read what the validator enforces, and diff the two sets.

**3. Partial `TensorMemoryLayout` coverage.** An op handling some memory layouts and silently
falling through on others. Absence of an `else` that fatals is the finding.

## Severity

`MUST-FIX` where the missing check yields wrong output or a hang — which is most of them, since a
clean `TT_FATAL` is the alternative being skipped. `SHOULD-FIX` where the unsupported case is
currently unreachable but nothing enforces that it stays so.

Note this is *not* a request for defensive fallbacks. Per `tt-review-core`, the correct fix is an
explicit fatal at the boundary, never a silent fallback that limps on.
