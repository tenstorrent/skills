#!/usr/bin/env bash
TT_MODEL_BRINGUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Runner-side gate for the agentic-evals stage. It reads the records the agent writes (results.json,
# agentic_serving.json), never a helper script's output, and reports every missing or short record. It blocks
# (critical) only where a score or a failure could be hidden: a phase with no scored run, an uncollected run, a
# voided list, a failed task with no cause, a task cut off by a limit that was not rerun higher, a rerun that
# changed the score for another reason. A phase not passed yet and everything else is advisory. A checker crash
# is critical.
# Exit 0 pass, 1 advisory, 2 critical, 3 error.
if [ -z "${MODEL_DIR:-}" ] && [ -z "${HF_MODEL:-}" ]; then
  echo "Neither MODEL_DIR nor HF_MODEL is set; cannot scope the check to the target model." >&2
  exit 3
fi
python3 "${TT_MODEL_BRINGUP_ROOT}/scripts/check_context_contract.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" \
  --stage agentic-evals --require-contract --optional-for-existing || rc=$?
rc=${rc:-0}
[ "$rc" -ge 2 ] && exit "$rc"

python3 "${TT_MODEL_BRINGUP_ROOT}/scripts/check_agentic_serving.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" --stage agentic-evals || serving=$?
[ "${serving:-0}" -gt "$rc" ] && rc=$serving
exit "$rc"
