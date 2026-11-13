#!/usr/bin/env bash
TT_MODEL_BRINGUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"
# Runner-side gate for the vLLM integration stage: served qualitative outputs
# (greedy and sampled) must exist and must not be mechanically degenerate. A
# serving path that doubles tokens or collapses to one token is a serving bug,
# not a model-quality limitation. Scoped to this run's model.
# Exit 0 pass, 1 advisory, 2 critical, 3 error.
if [ -n "${MODEL_DIR:-}" ]; then
  scope_args=(--model-dir "$MODEL_DIR")
elif [ -n "${HF_MODEL:-}" ]; then
  scope_args=(--hf-model "$HF_MODEL")
else
  echo "Neither MODEL_DIR nor HF_MODEL is set; cannot scope the check to the target model." >&2
  exit 3
fi
"$PYTHON" "${TT_MODEL_BRINGUP_ROOT}/runtime/readiness_check/check_degenerate_output.py" \
  "${scope_args[@]}" --missing-artifacts critical --scope vllm || exit $?

"$PYTHON" "${TT_MODEL_BRINGUP_ROOT}/scripts/check_context_contract.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" \
  --stage vllm --require-contract || rc=$?
rc=${rc:-0}
[ "$rc" -ge 2 ] && exit "$rc"

"$PYTHON" "${TT_MODEL_BRINGUP_ROOT}/scripts/check_serving_contract.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" --require-contract || contract=$?
[ "${contract:-0}" -ge 2 ] && exit "$contract"
[ "${contract:-0}" -gt "$rc" ] && rc=$contract

"$PYTHON" "${TT_MODEL_BRINGUP_ROOT}/scripts/check_prefill_features.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" --stage vllm || exit $?

"$PYTHON" "${TT_MODEL_BRINGUP_ROOT}/scripts/check_agentic_serving.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" --stage vllm || serving=$?
[ "${serving:-0}" -gt "$rc" ] && rc=$serving
# Stage usage: the runner writes `## Stage usage` and stage_usage.json before this check (2 if missing or malformed).
"$PYTHON" "${TT_MODEL_BRINGUP_ROOT}/scripts/stage_usage.py" check --model-dir "${MODEL_DIR:-}" --stage 09-vllm || usage=$?
[ "${usage:-0}" -gt "$rc" ] && rc=$usage
exit "$rc"
