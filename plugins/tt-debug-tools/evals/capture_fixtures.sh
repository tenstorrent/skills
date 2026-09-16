#!/usr/bin/env bash
# shellcheck disable=SC1087,SC2016,SC2086
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Capture real debug-tool output from a Tenstorrent device into test fixtures.
#
# Needs hardware, a built tt-metal, and TT_METAL_HOME. Run from anywhere:
#   TT_METAL_HOME=/path/to/tt-metal capture_fixtures.sh
#
# Each scenario writes cmd.sh, output.txt and meta.json into
# evals/<skill>/fixtures/<scenario>/. expected.json is authored by hand
# afterwards from output.txt — see evals/README.md.
#
# Sourcing this file defines the helpers without capturing anything, which is
# what a generated cmd.sh does to reuse them.

set -uo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
OUT_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
: "${TT_METAL_HOME:?set TT_METAL_HOME to a built tt-metal checkout}"

BIN="$TT_METAL_HOME/build/test/tt_metal"
EXAMPLES="$TT_METAL_HOME/build/programming_examples"
TRIAGE="$TT_METAL_HOME/tools/tt-triage.py"
# Triage needs ttexalens, which lives in tt-metal's virtualenv. MIN_ENV strips
# the environment down on purpose, so a bare `python3` resolves to whichever
# interpreter is first on PATH and fails the import.
TRIAGE_PY="$TT_METAL_HOME/python_env/bin/python"
PROVOKE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tt-triage/provoke"

# TT_METAL_HOME does not locate the runtime any more: without one of these the
# binaries abort in SetUp with "Root Directory is not set".
export TT_METAL_RUNTIME_ROOT="${TT_METAL_RUNTIME_ROOT:-$TT_METAL_HOME}"
# Otherwise logs_dir is the working directory, so where watcher.log and the
# Inspector logs land depends on where this was invoked from — and triage,
# looking elsewhere, reports a device with no Inspector data.
export TT_METAL_LOGS_PATH="${TT_METAL_LOGS_PATH:-$TT_METAL_HOME}"
WATCHER_LOG="$TT_METAL_LOGS_PATH/generated/watcher/watcher.log"
# A provoking kernel prints here immediately before parking. Watching the print
# rather than a host timer is what makes a hang capture deterministic: a cold JIT
# build takes minutes, so any fixed settle either races the build or guesses.
DPRINT_LOG="$TT_METAL_LOGS_PATH/generated/provoke-dprint.log"
# Each of these mutually excludes the others on device; a capture that leaves one
# set from a previous scenario silently corrupts the next one's output.
CONFLICTING=(TT_METAL_WATCHER TT_METAL_DPRINT_CORES TT_METAL_DEVICE_PROFILER
             TT_METAL_NOC_DEBUG_DUMP TT_METAL_CHECKPOINT
             TT_METAL_LIGHTWEIGHT_KERNEL_ASSERTS TT_METAL_LLK_ASSERTS)

# The literal watcher prints to its own stdout the moment it halts a core. The
# faulting watcher.log dump is not usable as a signal: in test mode the poll loop
# breaks out before its fflush, and MeshWatcherFixture truncates the log again on
# the way out.
FAULT_MARKER='Watcher detected NOC error and stopped device'
# Written once per completed watcher interval, and flushed, so it is a safe
# signal that the log holds a whole dump.
DUMP_MARKER='Dump #[0-9]+ completed'
# Printed by the provoking kernel on the line before the barrier it never leaves,
# so it means the device is stuck rather than still building kernels.
BARRIER_MARKER='MCAST_AT_BARRIER'
# Printed on the line before the init that ebreaks, and before the store that
# corrupts a mailbox. Same contract as the barrier marker: the state that follows
# is permanent, so the print means stopped rather than still compiling.
LLK_ASSERT_MARKER='LLK_ABOUT_TO_INIT_MISMATCHED_CB'
MAGIC_MARKER='CORE_MAGIC_CORRUPTED'
# Ceiling for a probe. Generous for triage on a healthy device, short enough that
# a probe waiting on a dead RPC does not stall the run.
PROBE_TIMEOUT=300

commit=$(git -C "$TT_METAL_HOME" rev-parse HEAD 2>/dev/null || echo unknown)
board=$(tt-smi -s 2>/dev/null | sed -n 's/.*"board_type"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        | head -1)
board="${board:-unknown}"

# triage's dump_configuration prints the whole environment, so anything exported
# in the capturing shell lands in a fixture committed to a public repo. Running
# the probe under a minimal environment keeps secrets out by construction rather
# than by redacting them afterwards.
#
# Single-quoted so the variables survive into the generated cmd.sh and expand
# when it runs: baking this machine's PATH into a committed file would ship the
# very identity the scrub exists to remove.
#
# TT_METAL_CACHE is pinned rather than dropped. Without it every capture builds
# kernels cold, which buries the tool's own output in JIT logs.
export TT_METAL_CACHE="${TT_METAL_CACHE:-$TT_METAL_LOGS_PATH/jit-cache}"
# Both assert flags are folded into the JIT compile hash, so enabling them re-JITs
# firmware and every kernel. Sharing one cache directory would make the assert
# scenario evict everything the other scenarios just built, and them evict it back
# — a cold rebuild on every capture, in both directions. It gets its own.
# A later assignment wins in `env -i`, so appending this overrides MIN_ENV's.
LLK_CACHE="$TT_METAL_LOGS_PATH/jit-cache-llk-asserts"
# Must stay on one line: eval treats an embedded newline as a command separator,
# and `env -i VAR=...` with no command prints the environment instead of running
# anything — which lands the whole PATH in the fixture.
MIN_ENV='env -i PATH="$PATH" HOME="$HOME" TT_METAL_HOME="$TT_METAL_HOME" TT_METAL_RUNTIME_ROOT="$TT_METAL_RUNTIME_ROOT" TT_METAL_LOGS_PATH="$TT_METAL_LOGS_PATH" TT_METAL_CACHE="$TT_METAL_CACHE"'

scrub() {
  # Machine-specific paths carry a username and the vendoring rules forbid
  # shipping either. Rewritten to shell variables rather than placeholders so a
  # scrubbed cmd.sh still runs anywhere TT_METAL_HOME is exported.
  local file="$1"
  python3 - "$file" "$TT_METAL_LOGS_PATH" "$TT_METAL_HOME" "$OUT_ROOT" "$SELF" \
           "$PROVOKE" "$TT_METAL_CACHE" "$HOME" <<'PY'
import re, sys
path, logs_path, metal_home, out_root, self_path, provoke, cache, home = sys.argv[1:9]
text = open(path, errors="replace").read()
# When the two roots are the same directory there is nothing to tell apart, and
# whichever name won the tie would be stamped on every path -- including
# python_env and tools/, which belong to TT_METAL_HOME by contract. Drop the
# logs alias so the surviving token is the one that is always right.
if logs_path == metal_home:
    logs_path = ""
# Longest first: TT_METAL_LOGS_PATH often sits inside TT_METAL_HOME.
for needle, token in sorted(
    ((logs_path, "$TT_METAL_LOGS_PATH"), (metal_home, "$TT_METAL_HOME"),
     (out_root, "$OUT_ROOT"), (self_path, "$CAPTURE_SCRIPT"),
     (provoke, "$PROVOKE"), (cache, "$TT_METAL_CACHE"), (home, "$HOME")),
    key=lambda pair: len(pair[0] or ""), reverse=True,
):
    if needle and needle != "/":
        text = text.replace(needle.rstrip("/"), token)
text = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "<REDACTED>", text)
open(path, "w").write(text)
PY
}

# Failures of the harness rather than of the tool under test. A fixture holding
# one of these reads as "the tool found nothing", which is indistinguishable from
# a healthy device once the file is on disk.
INFRA_FAILURES='Timeout waiting for Ethernet core service|Root Directory is not set|cannot create directories: Permission denied|Failed to determine TT-Metal root'

check_infra() {
  # check_infra <output-file> -> prints a note when the capture failed for a
  # reason that has nothing to do with the tool being captured.
  local hit
  hit=$(grep -Eo "$INFRA_FAILURES" "$1" 2>/dev/null | head -1)
  [ -n "$hit" ] && echo "capture is not usable: $hit" && return
  # An empty artifact is never a finding. The extractors that slice a live log
  # can legitimately come up empty depending on when the fault landed, and an
  # empty file on disk reads as "the tool said nothing".
  [ -s "$1" ] || echo "capture is not usable: artifact is empty"
}

write_meta() {
  # write_meta <dir> <exit-code> [note]
  python3 - "$1/meta.json" "$commit" "$board" "$2" "${3:-}" <<'PY'
import json, sys, datetime
path, commit, board, rc, note = sys.argv[1:6]
meta = {
    "tt_metal_commit": commit,
    "board": board,
    "exit_code": int(rc),
    "captured": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
}
if note:
    meta["note"] = note
json.dump(meta, open(path, "w"), indent=2)
PY
}

# Everything below writes cmd.sh, the runnable record of how a fixture was
# captured. It has to be readable: someone re-running a capture edits the
# timeout or the gtest filter, and both were invisible when this was one line.
#
# The environment is written out in full rather than as $MIN_ENV. It is the most
# important thing on the page -- a capture is only reproducible if you can see
# exactly what the process was given -- so it is formatted, not abbreviated.

ENV_PREFIX='env -i '

cmd_header() {
  # cmd.sh lives at evals/<skill>/fixtures/<scenario>/cmd.sh; capture script and
  # OUT_ROOT both root at evals/ — four ..s to reach it.
  #
  # Shellcheck disables are load-bearing: env args like 'PATH="$PATH"' are
  # single-quoted on purpose so the outer shell defers expansion until the
  # source line runs (SC2016). The sourced capture script is discovered by
  # variable (SC1090). OUT_ROOT stays unquoted so the array slot expands
  # correctly for capture_at_marker's positional args (SC2086).
  printf '#!/usr/bin/env bash\n# shellcheck disable=SC1090,SC2016,SC2086\n'
  printf '# regenerates output.txt; needs TT_METAL_HOME exported\n'
  printf 'CAPTURE_SCRIPT="${CAPTURE_SCRIPT:-%s}"\n' '$(dirname "$0")/../../../../capture_fixtures.sh'
  printf 'OUT_ROOT="${OUT_ROOT:-%s}"\n' '$(cd "$(dirname "$0")/../../.." && pwd)'
  printf 'source "$CAPTURE_SCRIPT"\n\n'
}

collapse() {
  # Call sites wrap long commands across lines; that indentation is not part of
  # the command.
  printf '%s' "$1" | tr -s ' \t\n' ' '
}

env_statement() {
  # An `env -i ...` statement as a command line: `env -i \` then one assignment
  # per line. Words split on spaces, which is safe because such a statement is
  # assignments and a path, never a quoted string containing a space.
  local words=() word
  for word in $(collapse "$1"); do words+=("$word"); done
  printf 'env -i \\\n'
  local i n=${#words[@]}
  for ((i = 2; i < n; i++)); do
    if [ "$i" -eq $((n - 1)) ]; then printf '  %s\n' "${words[$i]}"
    else printf '  %s \\\n' "${words[$i]}"; fi
  done
}

env_array() {
  # The same statement as an array assignment, for when it has to be passed as a
  # single argument. Each word is single-quoted so `PATH="$PATH"` reaches the
  # shell that runs the command intact -- the same deferral the one-line form
  # had -- and "${name[*]}" joins them back with spaces.
  local name="$1" words=() word
  for word in $(collapse "$2"); do words+=("$word"); done
  printf '%s=(\n  %s %s\n' "$name" "'${words[0]}'" "'${words[1]}'"
  local i n=${#words[@]}
  for ((i = 2; i < n; i++)); do printf "  '%s'\n" "${words[$i]}"; done
  printf ')\n\n'
}

readable_arg() {
  # An argument, quoted so it survives as one word.
  local arg
  arg="$(collapse "$1")"
  case "$arg" in
    "") printf '""' ;;
    # An argument carrying a double quote of its own -- an awk program -- keeps
    # the escaped form. Double quotes would let the shell running cmd.sh expand
    # its `$0` before awk saw it. Named variables are unescaped because `scrub`
    # rewrites this file afterwards and its $TT_METAL_LOGS_PATH has to expand.
    *\"*) printf '%q' "$arg" | sed 's/\\\$\([A-Za-z_][A-Za-z0-9_]*\)/$\1/g' ;;
    *[[:space:]]*) printf '"%s"' "$arg" ;;
    *) printf '%s' "$arg" ;;
  esac
}

write_cmd() {
  # write_cmd <dir> <command...>; a marker capture needs this file's helpers.
  local dir="$1"; shift
  local cmd rest
  cmd="$(collapse "$*")"
  {
    cmd_header
    # A trailing `; something` is a second statement and gets its own line.
    rest=""
    case "$cmd" in *"; "*) rest="${cmd#*; }"; cmd="${cmd%%; *}" ;; esac
    case "$cmd" in
      "$ENV_PREFIX"*) env_statement "$cmd" ;;
      *) printf '%s\n' "$cmd" ;;
    esac
    [ -n "$rest" ] && printf '%s\n' "$rest"
  } > "$dir/cmd.sh"
  chmod +x "$dir/cmd.sh"
  scrub "$dir/cmd.sh"
}

write_cmd_call() {
  # write_cmd_call <dir> <function> <arg>...
  #
  # One argument per line, and any argument that is an `env -i` command is
  # hoisted into an array above the call so its environment can be read and
  # edited. capture_at_marker takes the producer before the probe, which is the
  # order they are hoisted in.
  local dir="$1" fn="$2"; shift 2
  local names=(producer probe) hoisted=0 rendered=() arg name
  {
    cmd_header
    for arg in "$@"; do
      case "$(collapse "$arg")" in
        "$ENV_PREFIX"*)
          name="${names[$hoisted]:-command$hoisted}"
          env_array "$name" "$arg"
          rendered+=("\"\${$name[*]}\"")
          hoisted=$((hoisted + 1))
          ;;
        *) rendered+=("$(readable_arg "$arg")") ;;
      esac
    done
    printf '%s \\\n' "$fn"
    local n=${#rendered[@]} i=0
    for arg in "${rendered[@]}"; do
      i=$((i + 1))
      if [ "$i" -eq "$n" ]; then printf '  %s\n' "$arg"
      else printf '  %s \\\n' "$arg"; fi
    done
  } > "$dir/cmd.sh"
  chmod +x "$dir/cmd.sh"
  scrub "$dir/cmd.sh"
}

record() {
  # record <skill> <scenario> <command...>
  local skill="$1" scenario="$2"; shift 2
  local dir="$OUT_ROOT/$skill/fixtures/$scenario"
  mkdir -p "$dir"
  write_cmd "$dir" "$@"

  echo "capturing $skill/$scenario"
  # Tools under test halt cores on purpose, so a non-zero exit is the expected
  # path rather than a capture failure.
  ( eval "$*" ) >"$dir/output.txt" 2>&1
  local rc=$?
  scrub "$dir/output.txt"

  local note
  note=$(check_infra "$dir/output.txt")
  write_meta "$dir" "$rc" "$note"
  if [ -n "$note" ]; then
    echo "  UNUSABLE — $note" >&2
    return 1
  fi
  echo "  -> $dir/output.txt ($(wc -l <"$dir/output.txt") lines, exit $rc)"
}

wait_for() {
  # wait_for <file> <extended-regex> <timeout-secs> [pid]
  #
  # A fixed sleep cannot work here: the wait is dominated by JIT kernel
  # compilation, which is cache-dependent and unbounded, while the state to read
  # afterwards lasts a few hundred milliseconds. Both a short and a long guess
  # produce a plausible-looking fixture of the wrong device state.
  local file="$1" pat="$2" timeout="$3" pid="${4:-}"
  local ticks=0 limit=$((timeout * 4))
  while :; do
    if [ -f "$file" ] && grep -Eq -- "$pat" "$file"; then
      return 0
    fi
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
      echo "  probe: pid $pid exited before matching /$pat/" >&2
      return 2
    fi
    ticks=$((ticks + 1))
    if [ "$ticks" -ge "$limit" ]; then
      echo "  probe: timed out after ${timeout}s waiting for /$pat/" >&2
      return 1
    fi
    sleep 0.25
  done
}

# Probe exit code from the last capture_at_marker, which cannot return it: the
# return channel carries whether the marker arrived at all.
CAPTURE_RC=0

capture_at_marker() {
  # capture_at_marker <dir> <marker-file|-> <regex> <timeout> <producer> <probe|-> [freeze]
  #
  # <marker-file> is the file to watch, or `-` for the producer's own output.
  # <probe> is the command whose output becomes the artifact, or `-` when the
  # producer's own output is the artifact.
  # [freeze] defaults to `freeze`: the producer is SIGSTOPped once the marker
  # lands, so the probe reads a frozen device rather than racing a teardown that
  # takes the state down 250ms later. Pass `nofreeze` for a producer that holds
  # itself open — a stopped process cannot answer the Inspector RPC, and triage
  # without it reports a device it cannot see into.
  #
  # Nothing is written on a failed wait: once on disk, a capture of the wrong
  # device state is indistinguishable from a bad fixture.
  local dir="$1" marker_file="$2" pat="$3" timeout="$4" producer="$5" probe="$6"
  local freeze="${7:-freeze}"
  local producer_log="$dir/producer.txt"
  mkdir -p "$dir"

  # A marker left in the file by an earlier scenario matches immediately and the
  # probe then captures the previous run's artifact.
  [ "$marker_file" != "-" ] && rm -f "$marker_file"

  set +m
  { eval "$producer" >"$producer_log" 2>&1 & } 2>/dev/null
  local pid=$!
  [ "$marker_file" = "-" ] && marker_file="$producer_log"

  wait_for "$marker_file" "$pat" "$timeout" "$pid"
  local waited=$?
  if [ "$waited" -ne 0 ]; then
    stop_producer "$pid"
    return "$waited"
  fi

  [ "$freeze" = "freeze" ] && kill -STOP "$pid" 2>/dev/null
  if [ "$probe" = "-" ]; then
    cp "$producer_log" "$dir/output.txt"
    CAPTURE_RC=0
  else
    # Bounded: triage against a frozen producer connects to an Inspector RPC
    # that will never answer, and an unbounded probe hangs the whole capture.
    ( eval "timeout $PROBE_TIMEOUT $probe" ) >"$dir/output.txt" 2>&1
    CAPTURE_RC=$?
  fi
  scrub "$dir/output.txt"
  # After stop_producer, not before: a producer interrupted while it holds the
  # device prints its teardown traceback on the way out, and anything scrubbed
  # earlier would leave those paths in the file the abort path tells you to read.
  stop_producer "$pid"
  scrub "$producer_log"
  return 0
}

stop_producer() {
  # SIGINT before SIGTERM: a producer holding a mesh device closes it from an
  # interrupt handler, and a process killed while it still owns the device
  # leaves the boards unable to run the next scenario.
  local pid="$1" sig kid
  # The pid and its descendants, never a process group: the producer is started
  # from this script's own group, so signalling the group would signal the
  # capture. An orphan left holding /dev/tenstorrent wedges every later scenario,
  # so the descendants have to be named explicitly.
  local targets="$pid"
  for kid in $(pgrep -P "$pid" 2>/dev/null); do targets="$targets $kid"; done
  kill -CONT $targets 2>/dev/null
  # Returning while the producer still owns the device makes the next scenario's
  # health gate fail on a device that is merely busy, so escalate to the end
  # rather than giving up on a timeout.
  for sig in INT INT TERM KILL; do
    kill -0 "$pid" 2>/dev/null || break
    kill -"$sig" $targets 2>/dev/null
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 240 ]; do
      sleep 0.25
      waited=$((waited + 1))
    done
  done
  wait "$pid" 2>/dev/null
}

record_at_marker() {
  # record_at_marker <skill> <scenario> <marker-file|-> <regex> <timeout> <producer> <probe|->
  local skill="$1" scenario="$2"; shift 2
  local dir="$OUT_ROOT/$skill/fixtures/$scenario"
  mkdir -p "$dir"
  write_cmd_call "$dir" capture_at_marker "$dir" "$@"

  echo "capturing $skill/$scenario"
  if ! capture_at_marker "$dir" "$@"; then
    echo "  ABORTED — marker never arrived; see $dir/producer.txt" >&2
    write_meta "$dir" 1 "capture aborted: producer never reached the marker"
    rm -f "$dir/output.txt"
    return 1
  fi

  local note
  note=$(check_infra "$dir/output.txt"; check_infra "$dir/producer.txt")
  write_meta "$dir" "$CAPTURE_RC" "$note"
  if [ -n "$note" ]; then
    echo "  UNUSABLE — $note" >&2
    return 1
  fi
  # Kept only when something went wrong: on a good capture the producer's own
  # log is init chatter, it is not one of the four files a fixture is made of,
  # and committing it doubles the fixture for no reading.
  rm -f "$dir/producer.txt"
  echo "  -> $dir/output.txt ($(wc -l <"$dir/output.txt") lines, probe exit $CAPTURE_RC)"
}

clear_env() { for v in "${CONFLICTING[@]}"; do unset "$v"; done; }

health_gate() {
  # A scenario that aborts mid-run can leave the boards unable to enumerate, and
  # every later scenario then captures a failure that looks like its own. Stop
  # instead of writing them. Recovery is deliberately not automated here: a reset
  # is destructive and is the caller's call.
  # Not piped into grep -q: that exits on the first match, tt-smi takes SIGPIPE,
  # and pipefail then reports a healthy device as a failure.
  local listing
  listing=$(timeout 120 tt-smi -ls 2>&1)
  if ! grep -q 'Board Type' <<<"$listing"; then
    echo "device does not enumerate — stopping before the next scenario." >&2
    echo "recover the device, then re-run; already-captured scenarios are kept." >&2
    return 1
  fi
  # Enumeration is not health: the boards can list while ethernet remote IO is
  # dead, and every scenario then fails in fabric init instead of on its own
  # merits. This opens a device and moves data, which enumeration does not.
  # Bounded: a device wedged by a leftover holder does not fail the loopback, it
  # hangs it, and an unbounded gate then blocks the whole capture indefinitely.
  if ! timeout 120 "$EXAMPLES/metal_example_loopback" >/dev/null 2>&1; then
    echo "device enumerates but a loopback program fails or hangs — stopping." >&2
    echo "recover the device, then re-run; already-captured scenarios are kept." >&2
    return 1
  fi
}

main() {
  # ---- tt-watcher: a bad NoC write trips the sanitizer -----------------------
  # Two artifacts, because they are read differently: the console report is what
  # a developer sees, the log is the per-core table.
  clear_env; health_gate || return 1
  record_at_marker tt-watcher noc-sanitize-trip - "$FAULT_MARKER" 300 \
    "$MIN_ENV $BIN/unit_tests_debug_tools \
       --gtest_filter=MeshWatcherFixture.TensixTestWatcherSanitize" \
    -

  # Waiting on the log's own dump marker would capture the *first* dump, which
  # lands before the first kernel launch: every core at GW with a blank k_id map.
  # The fault marker on the producer's stdout is what puts the log at the fault.
  # Only the last block is kept — by then the log holds one per 250ms interval.
  # Guarded on /Dump #/ because the final record is often the short tail written
  # after the last separator, which yields a 17-line fixture at random.
  clear_env; health_gate || return 1
  record_at_marker tt-watcher last-dump-before-fault - "$FAULT_MARKER" 300 \
    "$MIN_ENV $BIN/unit_tests_debug_tools \
       --gtest_filter=MeshWatcherFixture.TensixTestWatcherSanitize" \
    "awk 'BEGIN{RS=\"-----\n\"} /Dump #/{block=\$0} END{printf \"%s\", block}' '$WATCHER_LOG'"

  # The first dump is a fixture in its own right: it is what an idle device looks
  # like, and reading it as a hang is the mistake it guards against.
  clear_env; health_gate || return 1
  record_at_marker tt-watcher first-dump-idle "$WATCHER_LOG" "$DUMP_MARKER" 300 \
    "$MIN_ENV $BIN/unit_tests_debug_tools \
       --gtest_filter=MeshWatcherFixture.TensixTestWatcherSanitize" \
    "cat '$WATCHER_LOG'"

  clear_env; health_gate || return 1
  # Not the dump marker: the first completed dump lands before any kernel is
  # launched, so it shows every core parked at GW with a blank k_id map. The test
  # itself polls until the log holds the waypoints it expects and says so.
  record_at_marker tt-watcher waypoints - 'All patterns found!' 300 \
    "$MIN_ENV $BIN/unit_tests_debug_tools \
       --gtest_filter=MeshWatcherFixture.TestWatcherWaypoints" \
    "cat '$WATCHER_LOG'"

  # ---- tt-dprint ------------------------------------------------------------
  clear_env; health_gate || return 1
  # Not unit_tests_debug_tools: its DPRINT fixture routes output to a memfd so the
  # test can read it back, so the console shows gtest chatter and no prints. This
  # example's kernels print, and to stdout like a user's would.
  record tt-dprint kernel-print \
    "$MIN_ENV TT_METAL_DPRINT_CORES=all $EXAMPLES/metal_example_noc_tile_transfer"

  # ---- tt-profiler ----------------------------------------------------------
  clear_env; health_gate || return 1
  record tt-profiler device-zones \
    "$MIN_ENV TT_METAL_DEVICE_PROFILER=1 $EXAMPLES/profiler/test_full_buffer >/dev/null 2>&1; \
     CSV=\$TT_METAL_HOME/generated/profiler/.logs/profile_log_device.csv; \
     head -2 \$CSV; grep 'TEST-FULL' \$CSV | head -20; tail -4 \$CSV"

  # ---- tt-noc-dump: multicast write with no barrier --------------------------
  clear_env; health_gate || return 1
  record tt-noc-dump missing-write-barrier \
    "$MIN_ENV TT_METAL_NOC_DEBUG_DUMP=1 $BIN/unit_tests_noc_debugging \
       --gtest_filter=NOCDebuggingFixture.McastOnlyWriteFlush"

  # ---- tt-triage: a real hang, held open for the whole probe ------------------
  # The positive fixture the healthy run is the control for. A declared multicast
  # destination count larger than the rectangle leaves atomic responses
  # permanently outstanding, so BRISC never leaves noc_async_atomic_barrier.
  # Unlike the sanitize case nothing tears the state down 1.5s later, which is
  # what makes the capture deterministic rather than a race.
  #
  # Last, and it stays last: it ends with the boards holding a stuck program, so
  # every later scenario would fail its health gate on damage this one did.
  clear_env; health_gate || return 1
  record_at_marker tt-triage mcast-ack-deficit "$DPRINT_LOG" "$BARRIER_MARKER" 600 \
    "$MIN_ENV HOLD_SECS=900 TT_METAL_DPRINT_CORES=0,0 TT_METAL_DPRINT_FILE=$DPRINT_LOG \
       $TT_METAL_HOME/python_env/bin/python $PROVOKE/live_hang_mcast_ack.py" \
    "$MIN_ENV $TRIAGE_PY $TRIAGE --llm-output --dev=all" \
    nofreeze

  # ---- tt-triage: an LLK assertion halts one compute thread ------------------
  # The modal shape of the assert hangs seen in CI. TRISC0 ebreaks inside an
  # unpacker configuration check; everything loud in the report — dataflow cores
  # on stalled buffers, ethernet counter mismatches, the host timeout — is
  # downstream of it. Its own cache: the flag is in the JIT compile hash.
  # Watcher stays off, or the assert reports itself and stops being this artifact.
  #
  # LLK asserts alone, without lightweight asserts. The pairing the skill
  # recommends does not build here: the two together push cq_prefetch past the
  # idle-erisc code region as soon as ttnn brings up fabric dispatch. Nothing is
  # lost by dropping it — triage recovers the assert expression, callstack and
  # locals from the ELF, so dump_lightweight_asserts is fully populated anyway.
  clear_env; health_gate || return 1
  record_at_marker tt-triage llk-srca-mismatch "$DPRINT_LOG" "$LLK_ASSERT_MARKER" 900 \
    "$MIN_ENV HOLD_SECS=900 TT_METAL_LLK_ASSERTS=1 \
       TT_METAL_DPRINT_CORES=0,0 TT_METAL_DPRINT_FILE=$DPRINT_LOG TT_METAL_CACHE=$LLK_CACHE \
       $TT_METAL_HOME/python_env/bin/python $PROVOKE/live_hang_llk_assert.py" \
    "$MIN_ENV $TRIAGE_PY $TRIAGE --llm-output --dev=all" \
    nofreeze

  # ---- tt-triage: a corrupt mailbox makes every callstack untrustworthy ------
  # The trust gate. check_core_magic reports the same core whose callstack is
  # parked, and the skill's read order says that callstack cannot be believed.
  # The store is local and four bytes wide; no firmware reads the field, and a
  # reset repopulates it.
  clear_env; health_gate || return 1
  record_at_marker tt-triage corrupt-core-magic "$DPRINT_LOG" "$MAGIC_MARKER" 600 \
    "$MIN_ENV HOLD_SECS=900 TT_METAL_DPRINT_CORES=0,0 TT_METAL_DPRINT_FILE=$DPRINT_LOG \
       $TT_METAL_HOME/python_env/bin/python $PROVOKE/live_hang_corrupt_magic.py" \
    "$MIN_ENV $TRIAGE_PY $TRIAGE --llm-output --dev=all" \
    nofreeze

  echo
  echo "captured into $OUT_ROOT"
  echo "now author expected.json in each directory from its output.txt"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main
fi
