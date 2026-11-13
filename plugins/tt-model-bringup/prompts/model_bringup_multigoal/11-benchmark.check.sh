#!/usr/bin/env bash
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
python3 -m benchmark_stage.check --model-dir "$model_dir" --hf-model "${HF_MODEL:-}" || exit 2
python3 "$TT_MODEL_BRINGUP_ROOT/scripts/check_context_contract.py" \
  --model-dir "$model_dir" --hf-model "${HF_MODEL:-}" --stage benchmark --require-contract --optional-for-existing || exit 2
python3 "$TT_MODEL_BRINGUP_ROOT/scripts/check_agentic_serving.py" \
  --model-dir "$model_dir" --hf-model "${HF_MODEL:-}" --stage benchmark || rc=$?
rc=${rc:-0}
[ "$rc" -ge 2 ] && exit "$rc"
# Stage usage: the runner writes `## Stage usage` and stage_usage.json before this check (2 if missing or malformed).
python3 "${TT_MODEL_BRINGUP_ROOT}/scripts/stage_usage.py" check --model-dir "$model_dir" --stage 11-benchmark || usage=$?
[ "${usage:-0}" -gt "$rc" ] && rc=$usage
exit "$rc"
