# Categories 2 and 3 — TRISC synchronisation and the tile_regs protocol

The three TRISCs (unpack, math, pack) are independent threads on the same core. They synchronise
two ways: CB `push_back`/`wait_front`, and `tile_regs` acquire/commit/wait/release for the DST
handoff.

The insight the whole category rests on: **unpack and pack run independently.** If unpack finishes
the current CB data and nothing gates the next operation, unpack races ahead even though pack has
not finished writing the previous output.

## 2.1 Sequential operations sharing a CB

When op A produces into a CB that op B consumes, the chain must be complete:

```
A (pack side):    cb_push_back(cb_intermediate, ...)
B (unpack side):  cb_wait_front(cb_intermediate, ...)
```

Check: either A's output policy includes `push_back` **and** B's input policy includes
`wait_front`, or explicit `cb_push_back` / `cb_wait_front` calls sit between the two helper calls.

**Failure:** unpack races ahead of pack and reads stale or partial data. Intermittent — it passes
sometimes, which is what makes it expensive.

## 2.2 NoWait input policies

`NoWaitNoPop` and `NoWaitPopAtEnd` skip the leading `cb_wait_front` for performance. That is only
safe when the data is guaranteed present:

- an explicit `cb_wait_front` precedes the helper call, or
- the data is pre-loaded (sharded tensor backed by a globally allocated CB), or
- this is the first consumer of data another thread already pushed, and a wait exists elsewhere.

None of those holding makes the policy unsafe. Same failure as 2.1.

## 2.3 Push/wait between helpers

Producer `PerTile`/`PerChunk` output against consumer `WaitAndPopPerTile`/`WaitAndPopPerChunk`
input synchronises implicitly. A `Bulk` output policy (pushes at the end) or a `NoWait` input
policy means you must verify the chain by hand. Trace the policies on both sides.

## 3.1 The tile_regs protocol

```
MATH:  tile_regs_acquire()  ->  compute into DST  ->  tile_regs_commit()
PACK:  tile_regs_wait()     ->  pack from DST     ->  tile_regs_release()
```

`acquire()` blocks until PACK has released DST. `commit()` signals data ready. `wait()` waits for
that signal. `release()` clears DST and signals MATH.

## 3.2 Balance

Every `acquire()` needs a matching `commit()`; every `wait()` a matching `release()`; and the cycle
counts must be equal.

- Missing `commit()` → PACK hangs forever on `wait()`.
- Missing `release()` → MATH hangs forever on the next `acquire()`.
- Unequal counts → deadlock on the extra cycle.

## 3.3 DST does not survive release

`tile_regs_release()` **clears DST**. Anything left there is gone.

For accumulation across iterations — reductions over K blocks, partial sums — partial results must
be spilled to an intermediate CB **before** release and reloaded next iteration.

Check: is there a spill before release? After reloading via `copy_tile()` or
`copy_block_matmul_partials()`, is the hardware re-initialised? `copy_tile` corrupts the SRCA
unpacker config, so a `reduce_init_short_with_dt()` or `mm_init_short_with_dt()` must follow before
compute continues. Is the accumulator CB large enough for the spill?

**Failures:** expecting DST to persist → zeros or wrong values. Missing re-init after reload →
garbage. Undersized accumulator → the spill blocks on `cb_reserve_back` → deadlock.

## Before flagging

Most helpers manage `tile_regs` internally, and the reduce helper's `Accumulate` handles the whole
spill/reload/re-init dance. Flag only for raw compute APIs, or where helpers and manual `tile_regs`
calls are mixed.
