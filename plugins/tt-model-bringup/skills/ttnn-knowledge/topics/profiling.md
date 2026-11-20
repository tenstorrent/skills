# Profiling, tracing for measurement, and watcher

## Measuring traced runs

- Open the device with `trace_region_size=0` where the revision supports it: traces then go into dynamic DRAM buffers, and capture fails if one could overlap replay memory, in which case reserve a region ([tracing](tracing.md)).
- Use the pattern: compile/warmup, synchronize, signpost start, execute warmed trace or warmed measured path, synchronize once, signpost end.
- Copy the newest Tracy ops CSV to the git-ignored `bringup/artifacts/<stage>/` and run `tt-perf-report` with start/end signposts for the measured window; only its report goes into the evidence directory.
- `tt-perf-report --csv` may expose filtered kernel time as `Device Time` in microseconds rather than raw nanoseconds; convert accordingly and note the column used.
- Profiling should capture warmed prefill and warmed decode separately.

## `tt-perf-report`

`tt-perf-report` is a Python CLI published separately from tt-metal. Install it in the active tt-metal Python environment, not in a random system Python:

```bash
python -m pip install tt-perf-report
tt-perf-report --help
```

The input is the post-processed Tracy ops CSV, usually named like `ops_perf_results_YYYY_MM_DD_HH_MM_SS.csv`. Generate that CSV with the normal Tracy profiler flow, for example:

```bash
python -m tracy -r -p -v -m pytest <test-path> -k "<selector>"
```

If Tracy collection fails, the LLM tech report notes a device-only fallback:

```bash
TT_METAL_DEVICE_PROFILER=1 pytest <test-path> -k "<selector>"
python tools/tracy/process_ops_logs.py --date
```

Copy the ops CSV to a raw directory that is not committed and write the reports into the evidence directory:

```bash
export ARTIFACT_DIR="models/autoports/<model>/doc/<stage>"
export RAW_DIR="bringup/artifacts/$(basename "$ARTIFACT_DIR")/tracy/<layer_kind_id>"   # git-ignored
mkdir -p "$RAW_DIR" && cp <ops_perf_results_*.csv> "$RAW_DIR/decode_ops.csv"
tt-perf-report "$RAW_DIR/decode_ops.csv" \
  --start-signpost PERF_DECODE \
  --end-signpost PERF_DECODE_END \
  --csv "$ARTIFACT_DIR/tracy/<layer_kind_id>/decode_perf_report.csv" \
  > "$ARTIFACT_DIR/tracy/<layer_kind_id>/decode_perf_report.console.log"
tt-perf-report "$RAW_DIR/decode_ops.csv" \
  --start-signpost PERF_DECODE \
  --end-signpost PERF_DECODE_END \
  --no-summary \
  > "$ARTIFACT_DIR/tracy/<layer_kind_id>/decode_perf_report.txt"
```

Keep advice enabled: the optimize stages work from it. A compact no-advice table (`--no-advice`) is a secondary
file with its own name. Some environments install only the module form; `python -m tt_perf_report.perf_report`
takes the same arguments.

Use the same shape for prefill, with `PERF_PREFILL` signpost names and `prefill_*` filenames. If the installed `tt-perf-report` version does not support one of these flags, run `tt-perf-report --help`, use the closest supported flag set, and record the exact command in the bringup log.

The `*_perf_report.txt` file is for the human-readable table. Do not redirect the stdout from a `--csv` run into that filename; `--csv` mode prints command/status boilerplate such as "Writing CSV output..." rather than the rendered report table. Keep that chatter in `*_perf_report.console.log` if it is useful for provenance.

When calculating latency from the report, prefer the filtered `tt-perf-report` CSV for the signposted window. Check the time column units. Some versions expose `Device Time` in microseconds; raw Tracy ops CSVs often expose `DEVICE KERNEL DURATION [ns]`. Record which column and unit you used.

Sanity-check durations against the benchmark wall time before computing roofline percentages: multi-second device
ops inside a sub-millisecond traced decode window mean the duration data is invalid; keep the raw CSV and report
the profiler failure. When the raw ops CSV has sane performance-model bandwidth columns, a labelled modeled
roofline fallback is `sum(PM BANDWIDTH [ns] for decode matmuls) / measured traced decode window`; otherwise leave
DRAM utilization unreported.

## Profiling large models and known tooling failures

- Do not run Tracy or the device profiler over a full all-layer model: dumps reach many GB, device-profiler buffers
  overflow and the measurement is distorted. Profile a reduced variant with one real layer of each kind and the
  real surrounding path (embeddings, final norm, LM head, sampling or token feedback, real KV-cache and page-table
  shapes, the same trace path), over one warmed traced decode replay or the smallest signposted window.
- "Too many source locations" or dropped device markers during Tracy enrichment: the profile was too broad; rerun
  the reduced-layer probe instead of the full stack.
- Transient CCL or fabric link errors right after a failed multi-device run: recover the devices per
  [tt-device-usage](../../tt-device-usage/SKILL.md) and retry once before treating it as hardware evidence.
- Profiler or watcher runs that ended in remote Ethernet, ARC or ERISC errors on a T3K: see the profiler section of
  [tt-device-usage](../../tt-device-usage/SKILL.md#profiler-runs-that-wedged-a-t3k).
- After a recovery, keep compact evidence (`*_perf_report.txt`, `*_perf_report.csv`, reduced summaries, benchmark
  JSON, logs, READMEs) and leave raw dumps out of copied-back artifacts: `*.tensorbin`, `*.pt`, `*.refpt`,
  `ops_perf_results.csv`, `prefill_decode_ops.csv`, `*_decode_ops.csv`, raw multi-GB Tracy CSVs, unless the
  operator asks for one.

## Watcher

- Watcher is fully supported on source builds; wheel-only environments may not have the same debug coverage.
- `TT_METAL_WATCHER=<n>` enables watcher and sets the polling interval in seconds unless the value has an `ms` suffix.
- `TT_METAL_LOGS_PATH=<dir>` redirects generated logs; watcher writes under `<dir>/generated/watcher/`.
- CI commonly uses `TT_METAL_WATCHER=2`, `TT_METAL_WATCHER_APPEND=1`, `TT_METAL_WATCHER_NOINLINE=1`, and `TT_METAL_WATCHER_DISABLE_ETH=1`.
- For decoder-layer artifact runs, prefer a unique `TT_METAL_LOGS_PATH` per command over appending unrelated runs into one log.
- Watcher overflowing the ACTIVE_ETH kernel config buffer: retry with `TT_METAL_WATCHER_DISABLE_ETH=1` and record
  the scoped limitation; on multi-chip meshes `TT_METAL_WATCHER_NOINLINE=1` with a short polling interval also
  helped ([multichip](multichip.md)).
- Watcher and device profiler/debug-print should be separate runs because they use overlapping debug resources.
- Watcher detects asserts, invalid NOC coordinates or addresses, CB out-of-bounds NOC transactions, L1 address overflow, stack overflow, hardware faults on supported targets, waypoint state, and active kernel ids.
- A watcher-clean run still may contain normal attach, dump, kernel id, and detach lines. It must not contain fatal watcher exceptions or suspicious stack/L1/NOC/CB/sanitize messages.
- Good false-positive evidence includes the exact watcher log line, why the address/core/state is benign, the commit or issue proving it is known, and a separate correctness/profiling run showing the decoder path is otherwise sound.
