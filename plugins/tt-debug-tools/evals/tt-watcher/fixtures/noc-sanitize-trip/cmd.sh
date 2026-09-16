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
  '$TT_METAL_HOME/build/test/tt_metal/unit_tests_debug_tools'
  '--gtest_filter=MeshWatcherFixture.TensixTestWatcherSanitize'
)

capture_at_marker \
  $OUT_ROOT/tt-watcher/noc-sanitize-trip \
  - \
  "Watcher detected NOC error and stopped device" \
  300 \
  "${producer[*]}" \
  -
