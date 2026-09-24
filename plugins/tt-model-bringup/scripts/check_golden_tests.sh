#!/usr/bin/env bash
set -euo pipefail
python "$(dirname "${BASH_SOURCE[0]}")/check_golden_tests.py" \
  --model-dir "${MODEL_DIR:?MODEL_DIR is required}" --stage "${GOLDEN_TEST_STAGE:?Stage is required}"
