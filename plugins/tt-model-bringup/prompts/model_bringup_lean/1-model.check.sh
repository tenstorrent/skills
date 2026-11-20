#!/usr/bin/env bash
TT_MODEL_BRINGUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python "${TT_MODEL_BRINGUP_ROOT}/scripts/check_scope.py" --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}"; scope_rc=$?
[ "$scope_rc" -ge 2 ] && exit "$scope_rc"
# Lean model goal: coherent free-running output, context contract, reference suite, memory ledger and
# the model record. Exit 0 pass, 1 advisory, 2 critical, 3 error.
if [ -n "${MODEL_DIR:-}" ]; then
  scope_args=(--model-dir "$MODEL_DIR")
elif [ -n "${HF_MODEL:-}" ]; then
  scope_args=(--hf-model "$HF_MODEL")
else
  echo "Neither MODEL_DIR nor HF_MODEL is set; cannot scope the check to the target model." >&2
  exit 3
fi
python "${TT_MODEL_BRINGUP_ROOT}/runtime/readiness_check/check_degenerate_output.py" \
  "${scope_args[@]}" --missing-artifacts critical --scope autoregressive || exit $?
python "${TT_MODEL_BRINGUP_ROOT}/scripts/check_context_contract.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" --stage full-model --require-contract || exit $?
rc=0
for check in check_reference_accuracy.py check_model_goal.py; do
  python "${TT_MODEL_BRINGUP_ROOT}/scripts/$check" --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" || r=$?
  [ "${r:-0}" -gt "$rc" ] && rc=$r; r=0
done
python "${TT_MODEL_BRINGUP_ROOT}/scripts/check_agentic_serving.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" --stage full-model || r=$?
[ "${r:-0}" -gt "$rc" ] && rc=$r
exit "$rc"
