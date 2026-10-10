---
name: measured-op-analysis
description: Run a tt-metal model test under Tracy and report the ops that actually executed, host versus device op time, and max cores per op, then diff against a static op table. Use for "measure the ops of model X", host fallback percentage, or tt-metal#58113 criteria 2 and 4a.
---

# Measured op analysis

Needs a Tenstorrent device (or ttsim for Quasar) and a built tt-metal checkout. Numbers come
from `<plugin-root>/scripts/tracy_report.py`, never from reading the CSV by eye.

`<plugin-root>` is the `tt-model-op-analysis` plugin directory, two levels above this
SKILL.md; resolve every `<plugin-root>/...` path against it.

## Steps

1. **Setup.** Follow `<plugin-root>/references/run-setup.md` with the same model and test as the
   static run you want to compare against.
2. **Device.** Read arch, `compute_with_storage_grid_size()` and `dram_grid_size()`; map to a
   profile with `<plugin-root>/references/targets.md`, else `custom`. Record in `run.json`.
3. **Warm run.** Check that the test runs the model at least twice. If not, offer either an
   existing warm variant of the test or a local, uncommitted edit that runs it twice with
   `from tracy import signpost; signpost("op-analysis-start")` before and
   `signpost("op-analysis-end")` after the second run. Without signposts the report is `cold`.
4. **Run.** Command: `python -m tracy -p -r -v -m "pytest <node id>"`. Use `tt-device-mcp` when
   the host has it; otherwise show the command and ask the user to run it. Do not set
   `TT_METAL_WATCHER` or `TT_METAL_DPRINT_CORES` with the profiler. If the profiler does not work
   on the target (for example ttsim), report the error and stop.
5. **Process.** Newest `ops_perf_results_*.csv` under `$TT_METAL_HOME/generated/profiler/reports/`:
   `python3 <plugin-root>/scripts/tracy_report.py <csv> <run dir> --start op-analysis-start --end op-analysis-end`
   (omit the flags for a cold run). Pass `--peak-dram-mb` only with a measured value.
6. **Diff.** Only for a warm window: static launches are per inference, a cold window is the
   whole test. If a static run exists for this model with the same profile:
   `python3 <plugin-root>/scripts/diff_reports.py --static <static run>/op_table.csv --measured <run dir>/measured_ops.csv --profile <p> --out <run dir>/diff.csv`.
   Report differences; do not edit the static table.
7. **Summary.** `summary.md`: model, test, SHA, device and profile, window label, the
   host-fallback row, top ops by device kernel time, max cores per op, diff counts per category.
8. **Upload.** Ask; if yes, follow `<plugin-root>/references/drive-upload.md`.

## Rules

- Revert any local test edit after the run and say so.
- A cold window's host times include compilation; label it, never present it as warm.
- Keep the raw ops CSV path in `run.json` (`ops_csv`); copy the file into the run directory.
- Do not claim a host % or footprint that the scripts did not produce.
