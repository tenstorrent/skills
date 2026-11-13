#!/usr/bin/env bash
TT_MODEL_BRINGUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"
# Runner-side gate for the optimized-full-model stage: optimization must not
# have removed or degraded free-running TT generation evidence. Scoped to this
# run's model. Exit 0 pass, 1 advisory, 2 critical, 3 error.
if [ -n "${MODEL_DIR:-}" ]; then
  scope_args=(--model-dir "$MODEL_DIR")
elif [ -n "${HF_MODEL:-}" ]; then
  scope_args=(--hf-model "$HF_MODEL")
else
  echo "Neither MODEL_DIR nor HF_MODEL is set; cannot scope the check to the target model." >&2
  exit 3
fi
"$PYTHON" "${TT_MODEL_BRINGUP_ROOT}/runtime/readiness_check/check_degenerate_output.py" \
  "${scope_args[@]}" --missing-artifacts critical --scope autoregressive || rc=$?
rc=${rc:-0}
[ "$rc" -ge 2 ] && exit "$rc"

# Stage usage: the runner writes `## Stage usage` and stage_usage.json before this check (2 if missing or malformed).
"$PYTHON" "${TT_MODEL_BRINGUP_ROOT}/scripts/stage_usage.py" check --model-dir "${MODEL_DIR:-}" --stage 07-optimized-full-model || usage=$?
[ "${usage:-0}" -gt "$rc" ] && rc=$usage
exit "$rc"
