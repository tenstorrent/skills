#!/usr/bin/env bash
# shellcheck disable=SC1090,SC2016,SC2086
# regenerates output.txt; needs TT_METAL_HOME exported
CAPTURE_SCRIPT="${CAPTURE_SCRIPT:-$(dirname "$0")/../../../capture_fixtures.sh}"
OUT_ROOT="${OUT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
source "$CAPTURE_SCRIPT"

env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  TT_METAL_HOME="$TT_METAL_HOME" \
  TT_METAL_RUNTIME_ROOT="$TT_METAL_RUNTIME_ROOT" \
  TT_METAL_LOGS_PATH="$TT_METAL_LOGS_PATH" \
  TT_METAL_CACHE="$TT_METAL_CACHE" \
  TT_METAL_DEVICE_PROFILER=1 \
  $TT_METAL_HOME/build/programming_examples/profiler/test_full_buffer
tail -30 $TT_METAL_HOME/generated/profiler/.logs/profile_log_device.csv
