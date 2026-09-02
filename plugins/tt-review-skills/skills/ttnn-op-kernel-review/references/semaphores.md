# Category 6 — semaphore coordination between cores

Applies to operations using multicast or inter-core signalling, not to simple
parallelised-same-work ops. The failures here are hangs and races.

## 6.1 Missing atomic barrier after increment

`noc_semaphore_inc` is an **async** NoC operation. Without `noc_async_atomic_barrier()` after it,
execution proceeds before the increment reaches the destination core.

Check: after every `noc_semaphore_inc`, is there a barrier before the next operation that depends on
the signal having landed?

**Failure:** race → intermittent hangs. These are the hardest bugs in the category to diagnose,
because reproduction is timing-dependent. Flag the missing barrier even when the code currently
passes.

## 6.2 Reset between iterations

Where a semaphore gates each iteration of a loop, it must be reset to its initial value at the start
of each iteration. Otherwise the accumulated value from earlier iterations makes
`noc_semaphore_wait` pass immediately.

Check: in loop bodies containing `noc_semaphore_wait`, is there a `noc_semaphore_set` at the top? Is
the reset value right — `INVALID` for wait-for-VALID patterns, `0` for counter patterns?

**Failure:** the wait passes immediately, data is consumed before it is ready. The symptom is
corruption or a hang *later*, not at the wait.

## 6.3 Multicast destination count

Check: is the sender inside the receiver grid? If yes, `num_dests = grid_size - 1` (exclude self);
if no, `num_dests = receiver_cores.size()`. Does `noc_semaphore_wait(sender_sem, N)` use the same N
as the number of cores calling `noc_semaphore_inc(sender_sem_addr, 1)`? Does
`noc_semaphore_set_multicast(..., num_dests)` use the right count?

**Failure:** off-by-one → the sender waits for a signal that never arrives, or signals a core that
does not exist and the signal lands at the wrong address.

The self-inclusion question is the usual source of the off-by-one. Check it explicitly rather than
assuming the grid excludes the sender.

## 6.4 Signal and wait totals must balance

```
signals_per_iteration x num_sender_cores x num_iterations
    == wait_threshold x num_receiver_cores x num_iterations
```

Check: what receiver count does the program factory pass to the sender kernel? What threshold does
the sender's `noc_semaphore_wait` use? Do they match?

**Failure:** imbalance → sender or receivers hang waiting for signals that never come.

## Evidence

Semaphore findings need both ends cited: the increment site and the wait site, or the factory line
setting the count and the kernel line consuming it. A finding naming only one end cannot be acted
on, because the fix could belong at either.
