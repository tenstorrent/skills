#!/usr/bin/env bash
TT_MODEL_BRINGUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Runner-side gate for goal 6b: the reference corpus, both comparisons, a match at full precision
# and the regression tiers exist. Exit 0 pass, 1 advisory, 2 critical, 3 error.
python "${TT_MODEL_BRINGUP_ROOT}/scripts/check_reference_accuracy.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}"
