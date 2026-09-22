#!/usr/bin/env bash
# shellcheck disable=SC1090,SC2016,SC2086
# Reproduces the comparison-fail log for a real ttnn matmul. The provoker
# lives under evals/tt-ttnn-flags/provoke/; run it under tt-metal's venv.
set -e
: "${TT_METAL_HOME:?set TT_METAL_HOME}"
cd "$TT_METAL_HOME"
python_env/bin/python \
  "$(dirname "$0")/../../provoke/run_graph_capture.py"
