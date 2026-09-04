#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Capture real debug-tool output from a Tenstorrent device into test fixtures.
#
# Needs hardware, a built tt-metal, and TT_METAL_HOME. Run from anywhere:
#   TT_METAL_HOME=/path/to/tt-metal capture_fixtures.sh /path/to/fixtures
#
# Each scenario writes cmd.sh, output.txt and meta.json. expected.json is
# authored by hand afterwards from output.txt — see fixtures/README.md.

set -uo pipefail

OUT_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fixtures}"
: "${TT_METAL_HOME:?set TT_METAL_HOME to a built tt-metal checkout}"

BIN="$TT_METAL_HOME/build/test/tt_metal"
EXAMPLES="$TT_METAL_HOME/build/programming_examples"
TRIAGE="$TT_METAL_HOME/tools/tt-triage.py"
# Each of these mutually excludes the others on device; a capture that leaves one
# set from a previous scenario silently corrupts the next one's output.
CONFLICTING=(TT_METAL_WATCHER TT_METAL_DPRINT_CORES TT_METAL_DEVICE_PROFILER
             TT_METAL_NOC_DEBUG_DUMP TT_METAL_CHECKPOINT
             TT_METAL_LIGHTWEIGHT_KERNEL_ASSERTS)

commit=$(git -C "$TT_METAL_HOME" rev-parse HEAD 2>/dev/null || echo unknown)
board=$(tt-smi -s 2>/dev/null | grep -i board_type | head -1 | tr -d '\r' || echo unknown)

record() {
  # record <skill> <scenario> <command...>
  local skill="$1" scenario="$2"; shift 2
  local dir="$OUT_ROOT/$skill/$scenario"
  mkdir -p "$dir"

  printf '#!/usr/bin/env bash\n# regenerates output.txt\n%s\n' "$*" > "$dir/cmd.sh"
  chmod +x "$dir/cmd.sh"

  echo "capturing $skill/$scenario"
  # Tools under test halt cores on purpose, so a non-zero exit is the expected
  # path rather than a capture failure.
  ( eval "$*" ) >"$dir/output.txt" 2>&1
  local rc=$?

  python3 - "$dir/meta.json" "$commit" "$board" "$rc" <<'PY'
import json, subprocess, sys, datetime
path, commit, board, rc = sys.argv[1:5]
json.dump({
    "tt_metal_commit": commit,
    "board": board,
    "exit_code": int(rc),
    "captured": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
}, open(path, "w"), indent=2)
PY
  echo "  -> $dir/output.txt ($(wc -l <"$dir/output.txt") lines, exit $rc)"
}

clear_env() { for v in "${CONFLICTING[@]}"; do unset "$v"; done; }

# ---- tt-noc-dump: multicast write with no barrier ----------------------------
clear_env
record tt-noc-dump missing-write-barrier \
  "TT_METAL_NOC_DEBUG_DUMP=1 $BIN/unit_tests_noc_debugging \
     --gtest_filter=NOCDebuggingFixture.McastOnlyWriteFlush"

# ---- tt-watcher: a bad NoC write trips the sanitizer -------------------------
clear_env
record tt-watcher noc-sanitize-trip \
  "TT_METAL_WATCHER=1 $BIN/unit_tests_debug_tools \
     --gtest_filter=MeshWatcherFixture.TensixTestWatcherSanitize; \
   cat \$TT_METAL_HOME/generated/watcher/watcher.log"

clear_env
record tt-watcher waypoints \
  "TT_METAL_WATCHER=1 $BIN/unit_tests_debug_tools \
     --gtest_filter=MeshWatcherFixture.TestWatcherWaypoints; \
   cat \$TT_METAL_HOME/generated/watcher/watcher.log"

# ---- tt-triage: read a halted core, and a healthy run as the control --------
# The sanitize case halts a core and leaves the process up, which is the state
# triage is built to read.
clear_env
record tt-triage halted-core \
  "$BIN/unit_tests_debug_tools \
     --gtest_filter=MeshWatcherFixture.TensixTestWatcherSanitize & \
   sleep 20; python3 $TRIAGE --llm-output --dev=all; wait"

clear_env
record tt-triage healthy-run \
  "$EXAMPLES/matmul_multi_core; python3 $TRIAGE --llm-output --dev=all"

# ---- tt-dprint --------------------------------------------------------------
clear_env
record tt-dprint kernel-print \
  "TT_METAL_DPRINT_CORES=0,0 $BIN/unit_tests_debug_tools \
     --gtest_filter=DevicePrintOutputFixture.PrintSimpleString"

# ---- tt-checkpoint ----------------------------------------------------------
clear_env
record tt-checkpoint cb-state \
  "TT_METAL_CHECKPOINT=1 TT_METAL_DPRINT_CORES=0,0 $BIN/unit_tests_debug_tools \
     --gtest_filter=DevicePrintCheckpointTest.DumpCB"

# ---- tt-profiler ------------------------------------------------------------
clear_env
record tt-profiler device-zones \
  "TT_METAL_DEVICE_PROFILER=1 $EXAMPLES/profiler/test_full_buffer; \
   tail -30 \$TT_METAL_HOME/generated/profiler/.logs/profile_log_device.csv"

echo
echo "captured into $OUT_ROOT"
echo "now author expected.json in each directory from its output.txt"
