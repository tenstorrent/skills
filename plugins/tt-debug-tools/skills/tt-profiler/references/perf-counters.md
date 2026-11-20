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
python -m tracy -p -r --profiler-capture-perf-counters all --perf-counter-multipass -m pytest <test> -k <filter>
```

`fpu` captures the FPU, SFPU and Math counters in one pass; there is no `sfpu`
group. All columns blank means no counters were captured: re-run with `fpu`.
`all` needs `--perf-counter-multipass`, which replays the workload once per
pass, so every pass must run the same ops.

Counters are latched per kernel zone, not sampled over time.

## Read the columns

| Column | Engine | What runs on it |
|---|---|---|
| `Avg FPU util on full grid (%)` | Matrix engine | matmul, tile matrix ops, reductions |
| `Avg SFPU util on full grid (%)` | Vector / special-function unit | activations, `exp`/`recip`/`sqrt`/`gelu`, eltwise, softmax |
| `Avg Math util on full grid (%)` | Math thread (TRISC1) | all math instructions |

Each is busy cycles ÷ kernel cycles × 100, averaged over the cores that
reported.
