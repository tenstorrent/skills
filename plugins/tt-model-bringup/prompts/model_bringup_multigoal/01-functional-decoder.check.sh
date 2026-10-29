#!/usr/bin/env bash
TT_MODEL_BRINGUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -z "${MODEL_DIR:-}" ] && [ -z "${HF_MODEL:-}" ]; then
  echo "Neither MODEL_DIR nor HF_MODEL is set; cannot scope the check to the target model." >&2
  exit 3
fi
python "${TT_MODEL_BRINGUP_ROOT}/scripts/check_stack_freshness.py" \
  --mode verify --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}"
