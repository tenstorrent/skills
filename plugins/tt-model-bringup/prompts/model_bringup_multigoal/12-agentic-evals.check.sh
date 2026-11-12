#!/usr/bin/env bash
TT_MODEL_BRINGUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"
# Runner-side gate for the agentic-evals stage: serving invariants still hold (autoport entry);
# each phase of the mode passed (score within the band of its reference, wall time explained by
# the stage-11 serving rates), its run was collected, every failed task has a cause, no list is
# voided and limits are never above the harness reference. An operator decision in inputs.json
# turns a failed phase into an advisory.
# Exit 0 pass, 1 advisory, 2 critical, 3 error.
if [ -z "${MODEL_DIR:-}" ] && [ -z "${HF_MODEL:-}" ]; then
  echo "Neither MODEL_DIR nor HF_MODEL is set; cannot scope the check to the target model." >&2
  exit 3
fi
"$PYTHON" "${TT_MODEL_BRINGUP_ROOT}/scripts/check_context_contract.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" \
  --stage agentic-evals --require-contract || rc=$?
rc=${rc:-0}
[ "$rc" -ge 2 ] && exit "$rc"

"$PYTHON" "${TT_MODEL_BRINGUP_ROOT}/scripts/check_agentic_serving.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" --stage agentic-evals || serving=$?
[ "${serving:-0}" -gt "$rc" ] && rc=$serving
exit "$rc"
