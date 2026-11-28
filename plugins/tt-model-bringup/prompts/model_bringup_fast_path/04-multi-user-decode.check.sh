#!/usr/bin/env bash
TT_MODEL_BRINGUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Runner-side gate for fast-path goal 4: doc/fast_path/multi_user_decode.json plus the context contract.
# Every check runs; the most severe exit wins. Exit 0 pass, 1 advisory, 2 critical, 3 error.
if [ -z "${MODEL_DIR:-}" ] && [ -z "${HF_MODEL:-}" ]; then
  echo "Neither MODEL_DIR nor HF_MODEL is set; cannot scope the check to the target model." >&2
  exit 3
fi
scope=(--model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}")
status=0
gate() {
  "$@"
  local rc=$?
  if [ "$rc" -gt "$status" ]; then status=$rc; fi
}
gate python "${TT_MODEL_BRINGUP_ROOT}/scripts/check_fast_path.py" "${scope[@]}" --phase multi_user_decode
gate python "${TT_MODEL_BRINGUP_ROOT}/scripts/check_context_contract.py" "${scope[@]}" \
  --stage fast-path --require-contract
exit "$status"
