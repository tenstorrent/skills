# False-positive guards

Findings a domain-loaded reviewer reliably gets wrong on Tenstorrent code. Check this list before
reporting anything in these shapes.

## TTNN memory: missing `deallocate` is not a leak

TTNN device buffers are freed when the last Python reference to the tensor is destroyed. A function
that allocates intermediates and returns without calling `ttnn.deallocate(...)` is not leaking.

**Report only when:** a reference demonstrably outlives its intended scope (stored on `self`,
appended to a list that persists, captured by a closure), or the tensor is kept alive indirectly and
you can show the path.

**Legitimate adjacent finding:** peak memory. "This holds four intermediates live simultaneously
inside the residual block; deallocating the first two before the matmul would cut peak L1" is a real
`SHOULD-FIX`. Say *peak memory*, not *leak* — the distinction changes what the author does.

## "Data parallel" means different things in vLLM and tt-metal

vLLM's `data_parallel_size` / `tt_data_parallel` may refer to the SDPA/KV-cache data-parallel
degree. tt-metal code often uses "data parallel" for mesh-local structure: input mesh rows,
attention weight copies. These are not the same axis.

Before flagging any relationship among `tt_data_parallel`, `max_batch_size`, `batch_size_per_row`,
mesh rows, mesh columns, and mesh world size, read the active caller and launch contract. This
mismatch has produced confident, wrong review comments on generator/vLLM integration code.

## Architecture divergence is often deliberate

Code that branches per architecture is usually correct and intentional. Do not flag it as
inconsistency, duplicated logic, or a missing abstraction unless you have checked that the
architectures actually behave the same way for the code path in question.

The inverse is also a finding, and a more serious one: code that does *not* branch where the
architectures genuinely differ. Several rules in these skills are architecture-specific — see
`tt-l1-memory-review`'s reduce constraints and `llk-race-audit-review`'s architecture scope. A diff
claiming support for an architecture it was not developed on is worth checking against those.

## Composite ops are not always the right answer

Preferring an existing composite op over a hand-rolled sequence is a good default, *not* a law. A
hand-rolled sequence can be correct when the composite carries a layout change, an unwanted
intermediate, or a fidelity difference. Check the op's actual behaviour before recommending the
swap, and phrase it as `CONSIDER` unless you have verified equivalence.

## A test that looks thin may be the right test

Do not demand exhaustive parameterisation on a test whose purpose is a single regression. Ask what
the test is for. Coverage findings belong to `tt-test-coverage-review`, which knows the bar.

## Do not review the whole file

Read the whole file for context — flag only what the diff changed, plus anything the diff genuinely
breaks. Pre-existing issues in surrounding code are out of scope unless the change makes them
materially worse. A review that relitigates untouched code is noise.

## Absence of a fallback is usually correct

The house style is to assert or error at the boundary rather than continue past an impossible
state. Do not ask for defensive handling of a `None` device or mesh — that is the anti-pattern this
review contract exists to catch, not a gap.
