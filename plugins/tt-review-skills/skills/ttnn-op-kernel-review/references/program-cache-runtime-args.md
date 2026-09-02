# Category 10b — runtime args and the cache-hit path

On a cache hit the framework reuses a compiled program and re-patches only what it was told to.
An address the override does not patch freezes at the first miss. Companion to
`references/program-cache-hashing.md`; both are distilled from merged tt-metal fixes.

## Smuggled buffer addresses

A buffer address written into a kernel's runtime args without going through the registration path
is never re-patched on a cache hit — it freezes at the first miss and the next call reads the wrong
buffer.

- **Raw `buffer->address()` in `SetRuntimeArgs`** inside `create()`, with no matching registration
  or `override_runtime_arguments()` patch.
- **Incomplete override**: patches some but not all buffer-derived RTA indices for that kernel.
  Check every address slot has a patch, not that a patch exists.
- **Optional-output aliasing frozen at miss time**: `auto& out = output.has_value() ?
  output.value() : input;` where the address is baked during `create()` and the aliasing decision is
  never re-evaluated on a hit with a different optional-output choice.
- **RTA vectors captured by value in `shared_variables`**, when what should be stored is the
  *indices* of the address slots so the override can patch them.

**The work-distribution trap.** If the custom hash drops shape or volume, one cached program is
shared across different work splits, and the per-core tile counts baked at the first miss are wrong
for the next call. A smuggled address was forcing a rebuild that silently covered this. So when a
diff *adds* a proper binding — or otherwise removes a rebuild — check that shape and volume are
actually in the hash. Fixing the smuggling can expose a latent hash bug.

## Rebuild on the cache-hit path

Calling `create_descriptor()` from `override_runtime_arguments()` pays the full cache-*miss* host
cost on every cache *hit*. Measured cliffs in the tens-of-times range.

- **Rebuild hidden behind a helper — the primary case.** The override body is short and clean but
  calls a helper that invokes `create_descriptor()`, `split_work_to_cores()`, or otherwise
  reconstructs the descriptor or core layout. **Trace every function the override calls; one level is
  rarely enough.** A textual pre-commit guard cannot see through indirection — a reviewer can. Treat
  "the override body looks fine" as the beginning of the check, not the end.
- **`apply_descriptor_runtime_args()` is not the same finding.** It applies args from a descriptor
  that already exists; its cost is the descriptor's size, not a rebuild. Applied against a minimal
  CB-only descriptor it is a cheap cache-hit repair, and in-tree ops use it that way deliberately.
  The finding is *what descriptor is being applied* — if the override builds a full one first, the
  rebuild is the bug and the apply is incidental.
- **Work-split logic duplicated** rather than shared — recomputing `split_work_to_cores`, core
  ranges or per-core counts inline. Both a host cost and a drift risk; the fix is one shared helper
  called by factory and override.
- **The no-op early-return trap.** An override that returns early when it has no hash-excluded
  scalars to write also skips **address and CB patching**. `override_runtime_arguments` *supersedes*
  the framework's own patching, so if it returns, nothing refreshes those addresses and they freeze
  at the first miss. Gate only the scalar writes; never gate address and CB patching.

## Severity

Wrong cache hit from a hash omission, or a frozen address, is `MUST-FIX` — wrong results, no error.
A rebuild on the hit path is `SHOULD-FIX` unless the measured cost is severe. Both are invisible in
a single-invocation test, so "the unit test passes" carries no weight here — ask for the
repeated-invocation test, which `tt-test-coverage-review` also requires.
