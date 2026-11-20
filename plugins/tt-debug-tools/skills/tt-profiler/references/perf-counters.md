# Math-engine counters and `PM FPU UTIL`

## `PM FPU UTIL (%)` is an estimate

It is `100 × PM COMPUTE [ns] / DEVICE KERNEL DURATION [ns]` from the op's perf
model; nothing measured the FPU. It fails both ways:

- Ops with no real perf model read ~0 while busy.
- Data-movement ops whose model bills their time as compute read high with no
  matrix work.

Do not call an op compute- or bandwidth-bound from it. Use the measured
counters below, or the per-RISC split in `op-report.md`.

## Capture the counters

```bash
python -m tracy -p -r --profiler-capture-perf-counters fpu -m pytest <test> -k <filter>
python -m tracy -p -r --profiler-capture-perf-counters fpu,pack,unpack,instrn -m pytest <test> -k <filter>
python -m tracy -p -r --profiler-capture-perf-counters all --perf-counter-multipass -m pytest <test> -k <filter>
```

- Groups: `fpu` (FPU, SFPU and Math counters together), `pack`, `unpack`,
  `instrn`, `l1_0`..`l1_5` (`l1_2` and up on Blackhole only), and `all` =
  fpu, pack, unpack, instrn plus every L1 group the arch has. There is no
  `sfpu` group: tracy warns and ignores it, and `fpu` already captures SFPU.
- The flag sets `TT_METAL_PROFILE_PERF_COUNTERS`; that variable set without the
  flag is ignored with a warning.
- One pass holds one L1 bank and a capped number of groups:
  `PERF_COUNTER_MAX_GROUPS_PER_PASS` in `tools/tracy/perf_counter_multipass.py`
  (5). A request that needs more passes fails with the pass plan unless
  `--perf-counter-multipass` is given, which replays the workload once per pass
  and merges the device logs. `all` needs one pass per L1 bank.
- Later-pass rows that match no op of pass 0 are dropped
  (`Dropped N perf-counter rows`), so every pass must run the same ops. Prefer
  the one-pass `fpu` unless the other groups are needed.
- `all` needs the arch: set `ARCH_NAME`, or tracy starts ttnn to detect it.

Counters are latched per kernel zone, not sampled over time. There is no
per-microsecond view.

## Read the columns

| Column | Engine | What runs on it |
|---|---|---|
| `Avg FPU util on full grid (%)` | Matrix engine | matmul, tile matrix ops, reductions |
| `Avg SFPU util on full grid (%)` | Vector / special-function unit | activations, `exp`/`recip`/`sqrt`/`gelu`, eltwise, softmax |
| `Avg Math util on full grid (%)` | Math thread (TRISC1) | all math instructions |

Each is busy cycles ÷ kernel cycles × 100, averaged over the cores that
reported. A matmul reads high FPU and low SFPU; a norm or activation the
reverse.

## When the columns are blank

- **All blank**: the run captured no counters. Re-run with `fpu`.
- **Blank on some ops**: op-to-zone matching failed. The raw per-core values are
  device-log events with zone id `9090`; `tools/tracy/perf_counter_analysis.py`
  builds the columns from them.
