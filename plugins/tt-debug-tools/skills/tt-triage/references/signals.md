# Signal to cause

A diagnosis needs three things together: **location** (device, core, RISC),
**state** (parked frame, or a failed check), and **context** (which op, which
kernel binary). One alone is not a verdict.

If several ops are active, filter to those not done and take the lowest op id.

## RISC names

Tensix data movement `BRISC`/`BR`, `NCRISC`/`NC`. Tensix compute `TRISC0..2` /
`TR0..2`. Active Ethernet `erisc`, `subordinate_erisc` — lowercase. Idle
Ethernet `ierisc`, `subordinate_ierisc`. DRAM `drisc`. Match the casing when
quoting a verdict.

## Parked frames

Predicates describe behaviour; source wording shifts. Grep
`tt-metal/tt_metal/hw/inc/api/dataflow/dataflow_api.h` for the current text.

| Top frame | Blocks while | Likely cause | Next |
|---|---|---|---|
| `cb_wait_front(cb, n)` | received − acked < n | producer under-pushed, or consumer asked for more than the work produces | compare producer push count against the consumer's `n` |
| `cb_reserve_back(cb, n)` | free pages < n | consumer under-popped, or `n` exceeds capacity | check consumer pops, and that `n` ≤ CB pages |
| `cb_pop_front` / `cb_push_back` | bookkeeping | rarely the block site | look one frame up |
| `noc_semaphore_wait(addr, val)` | `*addr != val` — strict equality | writer never reached `val`, **or overshot it** | find the writer; confirm the exact value and that nothing else writes that address |
| `noc_semaphore_wait_min(addr, val)` | `*addr < val` | cross-core semaphore never reached `val` | check the writer's NoC address and increment arithmetic |
| `noc_async_read_barrier()` | response count ≠ issued count | reads never returned — bad source address, congestion, hung target | compare counters; enable NoC sanitize; validate the source address |
| `noc_async_write_barrier()` | ack count ≠ issued count | non-posted writes never acked — bad destination, or target stuck | check the destination and that the issuing core's NoC index matches |
| a plain `while` spin, no sync primitive | — | a condition inside that kernel never becomes true | open the source at the frame and read the loop |
| a user kernel function | — | op-specific logic | read the surrounding loop |

Counter trap: the write-side software counter is the *acked* count, not the
issued count. A barrier exits on equality, so the normal hang state is issued
faster than acked.

## Converging signals

Build the verdict from the combination.

| When these converge | Cause | Present |
|---|---|---|
| `check_core_magic` corruption, or `check_binary_integrity` `.text` mismatch or restricted access | **bad memory write** | Mailbox and callstack reads are unreliable. Anchor on address arithmetic and find the kernel writing out of bounds. |
| Callstacks converge on the first-hung op; a core waits on a CB or semaphore whose counterpart never came | **missed synchronisation** | Name the kernel that should have pushed or signalled and why it did not. Give file, the broken handshake, the fix. |
| The op immediately before the first-hung op is a **MatMul** and the matmul throttle is unset | **current droop, downstream of configuration** | Launch-time environment, absent from any triage artifact — label it a candidate, not a verdict, unless you can read the effective value. Present the configuration fix *and* the op that triggered it. |
| `check_arc` uptime outlier, Ethernet links down, or sustained low clock or high temperature | **device went bad, usually still downstream of software** | Trace what drove it there. Conclude a hardware fault only when no software trigger is found. |
| Only triage's own artifacts — broken components, NoC mismatched state, PC-not-in-range with a continuation | **no triage-detectable fault** | The cause is elsewhere. Keep working the workload. |

**Multi-host:** the common shape is a stuck op whose counterpart lives on another
rank — a fabric peer that never sent its data. Apply the rows per rank, then
trace to the *peer's* code.

## Verdict phrasing

Name location, state and context together, and cite a path with a line resolved
from the callstack:

*"`BRISC`@(2,3) stuck in `cb_wait_front(cb_in0, 4)` — producer `NCRISC` pushed
only 2 pages; see the reader kernel at `<path>:<line>`."*

If several cores are stuck at different signals, name them all and label the
result a candidate rather than a verdict.
