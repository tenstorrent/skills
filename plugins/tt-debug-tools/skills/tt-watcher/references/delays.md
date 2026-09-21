# Injected delays, for reproducing a race

Watcher can stall chosen NoC operations on chosen cores by a fixed number of
cycles. A race that needs an unlucky interleaving to show up becomes reproducible
because you are choosing the interleaving.

This is the only part of watcher that changes what the kernel *does* rather than
only observing it. Treat a result under delays as a reproduction, not as a
measurement.

## The variables

`TT_METAL_WATCHER_DEBUG_DELAY=<cycles>` sets the stall. On its own it does
nothing: it needs a target, and a target is a core set plus a processor set, per
operation class.

The three classes are `READ_DEBUG_DELAY`, `WRITE_DEBUG_DELAY` and
`ATOMIC_DEBUG_DELAY`. Each takes the same pair of variables, built from the class
name:

| Variable | Value |
|---|---|
| `TT_METAL_<CLASS>_CORES` | Comma-separated logical coordinates, e.g. `0,0`. Ranges and `all` are accepted the same way `TT_METAL_DPRINT_CORES` accepts them. |
| `TT_METAL_<CLASS>_RISCVS` | Processor set, e.g. `BR`. Parsed by the part's own HAL, so the accepted names follow that part. Omit it and every processor is targeted. |

Worked example, from `watcher.rst`:

```bash
TT_METAL_WATCHER=1 TT_METAL_WATCHER_DEBUG_DELAY=10 \
TT_METAL_READ_DEBUG_DELAY_CORES=0,0 TT_METAL_WRITE_DEBUG_DELAY_CORES=0,0 \
TT_METAL_READ_DEBUG_DELAY_RISCVS=BR TT_METAL_WRITE_DEBUG_DELAY_RISCVS=BR \
  ./build/test/tt_metal/test_eltwise_binary
```

## Two hard preconditions

`rtoptions.cpp` asserts both when `TT_METAL_WATCHER_DEBUG_DELAY` is set:

1. `TT_METAL_WATCHER` must be enabled.
2. NoC sanitization must not be disabled — the delays are injected by the same
   instrumentation that does the checking, so turning the checks off removes the
   hook.

The assertion text for the second names
`TT_METAL_WATCHER_DISABLE_NOC_SANITIZE`, which is not a real variable. The one
that trips it is `TT_METAL_WATCHER_DISABLE_SANITIZE_NOC`.

## The name to use

Upstream's own `ClaudeCurriculum/docs/hangs.md` calls the delay variable
`TT_METAL_WATCHER_DELAY`. That name is not in `rtoptions.cpp`. Use
`TT_METAL_WATCHER_DEBUG_DELAY`; the short form is silently ignored, which looks
exactly like a race that would not reproduce.

## Choosing a target

Delay the side you suspect is winning, not the side that fails. A missing
barrier shows up when the *writer* is slowed and the reader proceeds on stale
data, so `WRITE_DEBUG_DELAY` on the producer core is the usual first attempt.
Start at a few tens of cycles: large values push the run into a watcher timeout
and tell you nothing about ordering.
