# tt-perf-report recipes and advice policy

Part of [optimize](../SKILL.md): detail moved out of the main skill file. Read only the sections in play.

## `tt-perf-report`

Do not use this section to collect vLLM serving-stage evidence. vLLM and optimized-vLLM stages intentionally skip Tracy, `tt-perf-report`, and device-profiler collection to protect T3K hardware.

Install and verify in the active tt-metal environment:

```bash
python -m pip install tt-perf-report
tt-perf-report --help
python -m tt_perf_report.perf_report --help
```

Some environments install only the module form; use `python -m tt_perf_report.perf_report` with the same arguments.

Generate a Tracy ops CSV for a signposted window:

```bash
python -m tracy -r -p -v -m pytest <test-path> -k "<selector>"
```

If Tracy collection fails, use the device-profiler fallback and post-process:

```bash
TT_METAL_DEVICE_PROFILER=1 pytest <test-path> -k "<selector>"
python tools/tracy/process_ops_logs.py --date
```

Known tooling-failure signatures and prescribed actions - do not burn hours rediscovering these:

- Tracy enrichment failing with "too many source locations" or dropped device markers on large models: the profile was too broad. Stop it, preserve the log, and rerun a reduced-layer probe with one layer per kind instead of the full stack. Do this reduced probe up front for full-model profiling rather than waiting for the full-stack profile to fail.
- Device-profiler-enabled serving appearing in a run: this should only happen accidentally, because vLLM serving stages must not use profiler collection. Kill leftover `EngineCore`/server processes and run at most one bounded health check. If `tt-smi -ls --local` or reset hangs, or if logs show remote Ethernet/ARC/ERISC failures (`Timeout waiting for Ethernet core service remote IO request`, `ETH core heartbeat check failed`, `Unexpected ERISC Response Flags`, `Read 0xffffffff from ARC scratch`, ARC lock/readback waits), stop profiler collection and run the T3K reset recovery procedure. Do not run a full serving-adapter profile followed by `ttnn.ReadDeviceProfiler(mesh)`. Preserve the logs as `hardware-profiler-limited`; reboot/re-acquire is a fallback after reset fails, not the first blocker conclusion.
- Watcher overflowing the ACTIVE_ETH kernel config buffer: retry with `TT_METAL_WATCHER_DISABLE_ETH=1` and record the scoped limitation.
- Transient CCL/fabric link errors immediately after a failed multi-device run: reset devices and retry once before treating it as hardware evidence.

Copy the final CSV into the artifact directory and run:

```bash
export ARTIFACT_DIR="models/autoports/<model>/doc/optimized_decoder"
cp <ops_perf_results_*.csv> "$ARTIFACT_DIR/tracy/<layer_kind_id>/decode_ops.csv"
tt-perf-report "$ARTIFACT_DIR/tracy/<layer_kind_id>/decode_ops.csv" \
  --start-signpost PERF_DECODE \
  --end-signpost PERF_DECODE_END \
  --csv "$ARTIFACT_DIR/tracy/<layer_kind_id>/decode_perf_report.csv" \
  > "$ARTIFACT_DIR/tracy/<layer_kind_id>/decode_perf_report.console.log"
tt-perf-report "$ARTIFACT_DIR/tracy/<layer_kind_id>/decode_ops.csv" \
  --start-signpost PERF_DECODE \
  --end-signpost PERF_DECODE_END \
  --no-summary \
  > "$ARTIFACT_DIR/tracy/<layer_kind_id>/decode_perf_report.txt"
```

Use the same pattern for prefill with `PERF_PREFILL` signposts and `prefill_*` filenames. If your installed `tt-perf-report` version uses different flags, run `tt-perf-report --help`, use the equivalent flags, and record the exact command. You'll have to add these signposts to your code, of course.

The `*_perf_report.txt` file is for the human-readable table. Do not redirect the stdout from a `--csv` run into that filename; `--csv` mode prints command/status boilerplate such as "Writing CSV output..." rather than the rendered report table. Keep that chatter in `*_perf_report.console.log` if it is useful for provenance.

`tt-perf-report` runs should keep advice enabled. If you also need a compact no-advice table, run that as a secondary command with a distinct filename and keep the advice-backed table in your work log and final reports.

Check time units before computing latency. Filtered `tt-perf-report` CSVs may expose `Device Time` in microseconds; raw Tracy ops CSVs often expose `DEVICE KERNEL DURATION [ns]`.

Sanity-check profiler durations against the benchmark wall time before computing roofline percentages. If a filtered `tt-perf-report` table claims multi-second device ops inside a sub-millisecond traced decode window, the duration data is invalid. Preserve the raw CSV and report the profiler failure explicitly. When the raw ops CSV has sane performance-model bandwidth columns, you may compute a labeled modeled roofline fallback from `sum(PM BANDWIDTH [ns] for decode matmuls) / measured traced decode window`; otherwise leave DRAM utilization unreported rather than publishing a bogus percentage.

## Advice Policy

For every actionable `tt-perf-report` recommendation:

- try it. If there is a good reason to reject it, record the reason;
- record before/after latency, PCC, and any watcher or correctness issue;
- keep it if it improves the target metric without unacceptable PCC or complexity;
- reject it only with evidence, then continue optimizing the rest of the decoder.

Avoid suppressing advice in the report used to guide optimization. When applicable advice remains untried, call that out as remaining work rather than implying the optimization pass is complete.
