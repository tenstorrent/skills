# Rank ops in a test with `tt-perf-report`

The Tracy wrapper writes a per-op CSV that the `tt-perf-report` CLI reads
as a ranked table — the workflow to reach for when the question is "which
op dominates?" rather than "what did the kernel do at nanosecond N?".

## Prerequisites

```bash
pip install tt-perf-report
```

Once per venv. Verify against
`docs/source/tt-metalium/tools/tracy_profiler.rst` for the recommended
version pin at the workspace's ref.

## Run

```bash
python -m tracy -p -r -v -m pytest <test_path> -k <filter> -v
```

`-p` profile, `-r` write the CSV report, `-v` verbose. Internally sets
`TT_METAL_DEVICE_PROFILER=1`. The pytest after `-m` runs whole; quote if
it carries spaces.

Each run lands in a fresh timestamped folder:

```
$TT_METAL_HOME/generated/profiler/reports/<timestamp>/
  ops_perf_results_<timestamp>.csv        # the ranked-report input
  profile_log_device.csv                  # the raw device zones
  <name>.tracy                            # Tracy GUI timeline
```

`<timestamp>` is the newest subdirectory under `generated/profiler/reports/`
by mtime.

## First-iter caveat

The first run through a test populates the program cache and its host times
are inflated. The test has to iterate at least twice; analyse only the
second iteration. This is not a `tt-perf-report` flag — it is how the test
must be written.

## Analyse

```bash
tt-perf-report $TT_METAL_HOME/generated/profiler/reports/<ts>/ops_perf_results_<ts>.csv
```

Ranked per-op table: op code, `DEVICE FW DURATION [ns]`, per-RISC durations,
core count, math fidelity, parallelisation strategy.

`tt-perf-report` crashes on some op codes it does not know — an unfamiliar
custom op, `Unknown math fidelity` on `HiFi3` rows in some versions. When
it does:

```bash
tt-perf-report --no-stacked <csv>
```

If it still bails, read the CSV with pandas — the columns are all in the
raw file:

```python
import pandas as pd
df = pd.read_csv("generated/profiler/reports/<ts>/ops_perf_results_<ts>.csv")
top = (df.groupby("OP CODE")["DEVICE FW DURATION [ns]"]
         .agg(["sum", "count", "mean"])
         .sort_values("sum", ascending=False).head(10))
```

## Bottleneck tags

For a top op, the RISC with the largest kernel duration relative to its
peers names the bound.

| Tag | Signal |
|---|---|
| `reader-bound` | BRISC ≳ TRISC1. Compute waits on tiles. Batch `noc_async_read`, deepen the input CB, or shard the input. |
| `compute-bound` | TRISC1 dominates. Legitimate ceiling, over-specified fidelity, or a cheaper op variant is available. |
| `writer-bound` | NCRISC dominates. Compute waits to drain output. Usually downstream pressure or an under-sized output CB. |
| `NOC-stall` | BRISC and NCRISC both high, TRISC low. Data movement saturates the NoC. |
| `under-parallelized` | Low `CORE COUNT` versus device grid, high per-core duration. Reach for sharding or a wider dispatch grid. |
| `host-dominated` | `FW` short but `HOST DURATION [ns]` large past the first iteration. Points to Python or dispatch overhead, not the kernel. |

## Matmul: op-level DRAM% × FLOPs%

`tt-perf-report` reports `DRAM %` and `FLOPs %` per matmul row and tags
suspect rows `SLOW`. That is a different question from the per-RISC tag
above — it names the op-level bound.

| DRAM % | FLOPs % | Bound |
|---|---|---|
| < 40 | < 40 | `SLOW`: overhead / sync — dispatch, barriers, under-sized blocks |
| ≥ 60 | < 40 | bandwidth-bound |
| < 40 | ≥ 60 | compute-bound |
| — | ≥ 70 | near peak |
| mid | mid | inspect per-RISC |
