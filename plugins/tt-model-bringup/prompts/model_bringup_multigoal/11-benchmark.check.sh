#!/usr/bin/env bash
# Runner-side gate for the benchmark stage. Exit 0 pass, 1 advisory, 2 critical, 3 error.
# The evidence check exits 1 for advisory answer statistics (extraction < 95%,
# blank-at-cap > 5%); the worst code wins.
set -uo pipefail
TT_MODEL_BRINGUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -n "${MODEL_DIR:-}" ]; then
  model_dir="$MODEL_DIR"
elif [ -n "${HF_MODEL:-}" ]; then
  slug=$(printf '%s' "$HF_MODEL" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/_/g; s/^_//; s/_$//')
  model_dir="models/autoports/$slug"
else
  echo "Neither MODEL_DIR nor HF_MODEL is set." >&2
  exit 3
fi
export PYTHONPATH="$TT_MODEL_BRINGUP_ROOT/runtime${PYTHONPATH:+:$PYTHONPATH}"
worst=0
merge() {
  local code=$1
  if [ "$code" -gt 3 ]; then code=2; fi
  if [ "$code" -gt "$worst" ]; then worst=$code; fi
}
out=$(python3 -m benchmark_stage.check --model-dir "$model_dir" --hf-model "${HF_MODEL:-}" 2>&1)
code=$?
printf '%s\n' "$out"
# Exit 1 is advisory only when the checker says so; a crash also exits 1.
if [ "$code" -eq 1 ] && ! printf '%s' "$out" | grep -q 'advisory finding(s)'; then code=2; fi
merge "$code"
python3 "$TT_MODEL_BRINGUP_ROOT/scripts/check_context_contract.py" \
  --model-dir "$model_dir" --hf-model "${HF_MODEL:-}" --stage benchmark --require-contract
code=$?
# The context check exits nonzero only for a critical contract defect.
if [ "$code" -ne 0 ]; then merge 2; fi
exit "$worst"
