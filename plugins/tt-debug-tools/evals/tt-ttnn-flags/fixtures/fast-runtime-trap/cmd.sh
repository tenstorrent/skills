#!/usr/bin/env bash
# shellcheck disable=SC1090,SC2016,SC2086
# Reproduces the trap: comparison mode enabled while fast runtime mode is still
# on. tt-metal warns loudly. The captured CONFIG shows both set together.
set -e
: "${TT_METAL_HOME:?set TT_METAL_HOME}"
cd "$TT_METAL_HOME"
TTNN_CONFIG_OVERRIDES='{"enable_graph_report": true, "enable_comparison_mode": true}' \
  python_env/bin/python -c 'import ttnn'
