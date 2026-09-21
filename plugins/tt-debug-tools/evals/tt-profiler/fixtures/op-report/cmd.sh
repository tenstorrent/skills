#!/usr/bin/env bash
# shellcheck disable=SC1090,SC2016,SC2086,SC2012
# Reproduces ops_perf.csv from a tt-metal ttnn pytest under tracy.
# Requires TT_METAL_HOME on a built checkout and tt-perf-report installed
# in the same venv (uv pip install tt-perf-report).
set -e
: "${TT_METAL_HOME:?set TT_METAL_HOME}"
cd "$TT_METAL_HOME"
python_env/bin/python -m tracy -p -r -v -m pytest \
  tests/ttnn/unit_tests/operations/matmul/test_matmul_batch_mismatch.py::test_matmul_a_batch1_b_batched -v
LATEST=$(ls -td generated/profiler/reports/*/ | head -1)
CSV=$(ls "$LATEST"/ops_perf_results_*.csv)
python_env/bin/tt-perf-report "$CSV"
