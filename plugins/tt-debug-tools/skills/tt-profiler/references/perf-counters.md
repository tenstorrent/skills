# Math-engine counters and `PM FPU UTIL`

The ops-perf CSV (`op-report.md`) carries two kinds of utilisation column. One
is a model estimate, the other is measured. Reading the estimate as a
measurement sends optimisation the wrong way.

## `PM FPU UTIL (%)` is an estimate

It is `100 × PM COMPUTE [ns] / DEVICE KERNEL DURATION [ns]` from the op's perf
model; nothing measured the FPU. It fails both ways:

- Ops with no real perf model read ~0 while busy (observed: `MinimalMatmul`
  0.004%).
- Data-movement ops whose model bills their time as compute read high with no
  matrix work (observed: `Slice` 91.7%, `Permute` 75.2%, `AllGatherAsync`
  72.3%, `Transpose` over 100% on Blackhole).

Do not call an op compute- or bandwidth-bound from it. Use the measured
counters below, or the per-RISC split in `op-report.md`. For NoC, DRAM and
Ethernet bandwidth, which are blank without tt-npe: `noc-bandwidth.md`.

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
- The flag sets `TT_METAL_PROFILE_PERF_COUNTERS`. Newer trees ignore that
  variable, with a warning, when it is set without the flag.
- One pass holds one L1 bank and a capped number of groups:
  `PERF_COUNTER_MAX_GROUPS_PER_PASS` in `tools/tracy/perf_counter_multipass.py`
  (5 on current main, 3 on older trees). Read it in the workspace. A request
  that needs more passes fails with the pass plan unless
  `--perf-counter-multipass` is given, which replays the workload once per pass
  and merges the device logs. `all` needs one pass per L1 bank.
- Later-pass rows that match no op of pass 0 are dropped
  (`Dropped N perf-counter rows`), so every pass must run the same ops. Prefer
  the one-pass `fpu` unless the other groups are needed.
- `all` needs the arch: set `ARCH_NAME`, or tracy starts ttnn to detect it.
- Counter capture turns off the C++ post-processing on current main: the report
  logs `Falling back to legacy Python processing` and takes longer. Some older
  trees keep the C++ path.

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
- Or fall back to achieved FLOP/s: for a matmul, `2·M·K·N ÷ DEVICE FW DURATION
  [ns]` is GFLOP/s. Divide by the part's peak at the op's `MATH FIDELITY`;
  `tt-perf-report`'s `FLOPs %` does this for matmul rows.
