#!/usr/bin/env bash
# shellcheck disable=SC1090,SC2016,SC2086
# regenerates output.txt; needs TT_METAL_HOME exported
CAPTURE_SCRIPT="${CAPTURE_SCRIPT:-$(dirname "$0")/../../../capture_fixtures.sh}"
OUT_ROOT="${OUT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
source "$CAPTURE_SCRIPT"

producer=(
  'env' '-i'
  'PATH="$PATH"'
  'HOME="$HOME"'
  'TT_METAL_HOME="$TT_METAL_HOME"'
  'TT_METAL_RUNTIME_ROOT="$TT_METAL_RUNTIME_ROOT"'
  'TT_METAL_LOGS_PATH="$TT_METAL_LOGS_PATH"'
  'TT_METAL_CACHE="$TT_METAL_CACHE"'
  'HOLD_SECS=900'
  'TT_METAL_LLK_ASSERTS=1'
  'TT_METAL_DPRINT_CORES=0,0'
  'TT_METAL_DPRINT_FILE=$TT_METAL_HOME/generated/provoke-dprint.log'
  'TT_METAL_CACHE=$TT_METAL_HOME/jit-cache-llk-asserts'
  '$TT_METAL_HOME/python_env/bin/python'
  '$PROVOKE/live_hang_llk_assert.py'
)

probe=(
  'env' '-i'
  'PATH="$PATH"'
  'HOME="$HOME"'
  'TT_METAL_HOME="$TT_METAL_HOME"'
  'TT_METAL_RUNTIME_ROOT="$TT_METAL_RUNTIME_ROOT"'
  'TT_METAL_LOGS_PATH="$TT_METAL_LOGS_PATH"'
  'TT_METAL_CACHE="$TT_METAL_CACHE"'
  '$TT_METAL_HOME/python_env/bin/python'
  '$TT_METAL_HOME/tools/tt-triage.py'
  '--llm-output'
  '--dev=all'
)

capture_at_marker \
  $OUT_ROOT/tt-triage/llk-srca-mismatch \
  $TT_METAL_HOME/generated/provoke-dprint.log \
  LLK_ABOUT_TO_INIT_MISMATCHED_CB \
  900 \
  "${producer[*]}" \
  "${probe[*]}" \
  nofreeze
