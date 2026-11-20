#!/usr/bin/env bash
# Lean serve goal: the stage 9, 10 and 11 gates in order; the worst result wins, a critical stops.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../model_bringup_multigoal" && pwd)"
rc=0
for gate in 09-vllm 10-optimized-vllm 11-benchmark; do
  bash "$DIR/$gate.check.sh"; r=$?
  [ "$r" -ge 2 ] && exit "$r"
  [ "$r" -gt "$rc" ] && rc=$r
done
exit "$rc"
