# Categories 7 and 8 — control-flow CB balance and in-place misuse

## 7.1 Conditional branch balance

If a CB operation appears in one branch of a conditional but not the other, CB state diverges by
path.

Check: for every `if`/`else` containing CB operations, do both branches perform the same operations
on the same CBs with the same tile counts? If not, is the asymmetry intentional — a first iteration
differing from later ones is legitimate, provided overall loop balance still holds.

**Failure:** the CB page count drifts across iterations and eventually deadlocks, producer blocked
on a full CB or consumer blocked on an empty one. The deadlock happens many iterations after the
imbalance, so the stack trace points nowhere near the bug.

## 7.2 Early return cleanup

An early return must leave CB and `tile_regs` state consistent.

- Before any CB operations have started — safe, nothing held.
- After `cb_wait_front` but before the matching `cb_pop_front` — **unsafe**, pages held and never
  released.
- After `tile_regs_acquire` but before `tile_regs_commit` — **unsafe**, DST locked and PACK will
  hang.

The zero-work early return from category 5.1 is the common case. Check *where* it sits: correct at
the top of the kernel, a new bug if placed after the first wait.

## 7.3 Loop count agreement

Check: if `cb_wait_front` is in a loop of N iterations and `cb_pop_front` in a loop of M, does N ==
M? If a producer loop pushes P tiles and a consumer loop waits for C, does P == C? Watch `<` versus
`<=`, and `num_tiles` versus `num_tiles - 1`.

**Failure:** N > M → the consumer holds pages it never releases and the producer eventually blocks.
M > N → the consumer waits for pages that never arrive.

## 8 In-place operation

In-place means the **`PackTile` output CB uses the same index as one of the chain's input CBs**.
There are no `*_in_place` helper variants, and routing the output to a third CB and copying back is
wrong — flag it.

**Why aliasing works — phase ordering, not aliasing detection.** Each outer iteration runs a fixed
sequence: `tile_regs_acquire` → compute phase (per input: wait, exec, pop) → `commit` → `wait` →
pack phase (per output: reserve, pack, push) → `release`. The input's `cb_pop_front` fires in the
compute phase, always before the pack phase's `cb_reserve_back`. So an output CB aliasing an input
frees the slot before the reserve. The chain does not inspect output-versus-input equality; safety
falls out of the ordering. Its only same-buffer special case is `CbA == CbB` for the two *inputs*
(e.g. `square = x*x`), which dedups the B-side wait and pop.

**This holds only under per-tile streaming input lifecycles.** Under `Bulk`, `Held*`, or pop-at-end
policies the input is not popped before the reserve, and an aliasing output deadlocks.

Check: for every `add` / `sub` / `mul` one-liner or `BinaryFpu` chain element, and every `square`,
is the output CB index the same as an input CB index? Check the design document's CB assignment too
— two semantically distinct CBs sharing an index are the same physical CB.

Prerequisites for the same-buffer case: `cb_a` exclusively owned by the compute kernel with no
concurrent reader/writer push or pop; all Ht x Wt tiles of A present before the call; `cb_a` sized
for at least Ht x Wt tiles.

**Failure modes under a non-streaming lifecycle:** aliasing a held `icb_a` → `cb_reserve_back` on a
CB with un-popped tiles → pointer corruption. Aliasing `icb_b` under a bulk wait → no free pages to
reserve → deadlock. Working for small tile counts proves nothing; it is UB either way.

**Scope:** this covers binary helpers. SFPU unary helpers may support in-place where their CB
synchronisation allows it — check the specific helper before assuming either way.
