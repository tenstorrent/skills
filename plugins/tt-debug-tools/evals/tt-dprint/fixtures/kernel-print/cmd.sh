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
  TT_METAL_DPRINT_CORES=all \
  $TT_METAL_HOME/build/programming_examples/metal_example_noc_tile_transfer
