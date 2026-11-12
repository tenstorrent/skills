#!/usr/bin/env bash
TT_MODEL_BRINGUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Runner-side gate for the datatype-sweep stage: the selected precision policy
# must be within noise of the highest-precision reference on long reasoning
# text (probe precision_reasoning, readiness_check.run_precision_reasoning), or
# its loss must be a recorded limitation. Scoped to this run's model.
# Exit 0 pass, 1 advisory, 2 critical, 3 error.
if [ -z "${MODEL_DIR:-}" ] && [ -z "${HF_MODEL:-}" ]; then
  echo "Neither MODEL_DIR nor HF_MODEL is set; cannot scope the check to the target model." >&2
  exit 3
fi
python "${TT_MODEL_BRINGUP_ROOT}/scripts/check_eval_readiness.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" --stage datatype-sweep
