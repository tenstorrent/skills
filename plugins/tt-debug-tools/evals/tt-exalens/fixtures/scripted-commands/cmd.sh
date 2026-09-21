#!/usr/bin/env bash
# shellcheck disable=SC1090,SC2016,SC2086
# regenerates output.txt; needs TT_METAL_HOME exported
CAPTURE_SCRIPT="${CAPTURE_SCRIPT:-$(dirname "$0")/../../../../capture_fixtures.sh}"
OUT_ROOT="${OUT_ROOT:-$(cd "$(dirname "$0")/../../.." && pwd)}"
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
  'TT_METAL_DPRINT_CORES=0,0'
  'TT_METAL_DPRINT_FILE=$TT_METAL_HOME/generated/provoke-dprint.log'
  '$TT_METAL_HOME/python_env/bin/python'
  '$OUT_ROOT/tt-exalens/provoke/live_exalens_magic.py'
)

probe=(
  'env' '-i'
  'PATH="$PATH"'
  'HOME="$HOME"'
  'TT_METAL_HOME="$TT_METAL_HOME"'
  'TT_METAL_RUNTIME_ROOT="$TT_METAL_RUNTIME_ROOT"'
  'TT_METAL_LOGS_PATH="$TT_METAL_LOGS_PATH"'
  'TT_METAL_CACHE="$TT_METAL_CACHE"'
  '$TT_METAL_HOME/python_env/bin/tt-exalens'
  '--commands="device;'
  'brxy'
  '0,0'
  '0x50000'
  '4;'
  'exit"'
)

capture_at_marker \
  $OUT_ROOT/tt-exalens/fixtures/scripted-commands \
  $TT_METAL_HOME/generated/provoke-dprint.log \
  EXALENS_MAGIC_WRITTEN \
  600 \
  "${producer[*]}" \
  "${probe[*]}" \
  nofreeze
