# Category 4 — circular-buffer ownership and undefined behaviour

The CB API has invariants that, when broken, produce **undefined behaviour** — not an error, not a
hang, but silent memory corruption or non-deterministic failure. Everything here is `MUST-FIX`.

## 4.1 Single producer, single consumer

Each CB has exactly one producer thread and one consumer thread. The internal read/write pointers
do not support concurrent access.

Check: is `cb_push_back` (or `cb_reserve_back`) called from more than one RISC-V thread — both
BRISC and NCRISC pushing to the same CB? Is `cb_wait_front` (or `cb_pop_front`) called from more
than one? In the compute kernel, do multiple helpers consume the same CB without serialisation?
Sequential helpers are fine; they run one at a time.

**Failure:** internal pointer corruption. Intermittent corruption or non-deterministic hangs.

## 4.2 Tile count must evenly divide CB size

Tile counts in all `cb_*` calls must evenly divide the CB's total page capacity. A CB with 8 pages
and a `cb_wait_front(cb, 3)` is UB.

Check: for each CB, what is `num_pages` in the program descriptor? What tile counts appear in
`cb_wait_front`, `cb_pop_front`, `cb_reserve_back`, `cb_push_back`? Does `num_pages % tile_count ==
0` hold for every call?

Cite both the descriptor line and the offending call — a finding with only one end is not
actionable.

## 4.3 Consistent wait counts

All `cb_wait_front` calls on the same CB in the same kernel must use the same tile count.
Consecutive waits without an intervening pop are cumulative and must increment by that count.

```cpp
// CB has 8 pages, base count 4.
cb_wait_front(cb, 4);   // fine
cb_wait_front(cb, 8);   // fine: cumulative 4 + 4
cb_wait_front(cb, 12);  // UB unless the previous wait was 8

cb_wait_front(cb, 4);
cb_wait_front(cb, 12);  // UB: increment is 8, base is 4
```

## 4.4 Push/pop inside the reserve/wait window

- `cb_pop_front` only after a corresponding `cb_wait_front`, and not after a previous pop already
  released those tiles.
- `cb_push_back` only after a corresponding `cb_reserve_back`.
- `get_read_ptr()` is valid only between `cb_wait_front` and `cb_pop_front`.
- `get_write_ptr()` is valid only between `cb_reserve_back` and `cb_push_back`.
- Push/pop counts must not exceed the matching reserve/wait counts.

Check for a pop with no preceding wait, a push with no preceding reserve, and `get_read_ptr()` used
after `cb_pop_front` — that pointer is stale.

**Failure:** all UB. Silent corruption, stale reads, non-deterministic behaviour.

## 4.5 Wrapping

If a pop or push wraps the internal circular pointer past the buffer boundary, that is UB. It is
hard to catch statically; 4.2 is the practical guard. If tile counts divide the CB size, wrapping UB
cannot occur.

## Framing findings here

Say plainly that the behaviour is undefined rather than "may cause issues". And when the author
replies that it works, the answer is that UB frequently works for small tile counts and fails
non-deterministically at larger shapes — passing today is not evidence.
