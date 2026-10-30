#!/usr/bin/env bash
TT_MODEL_BRINGUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
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
python "${TT_MODEL_BRINGUP_ROOT}/runtime/readiness_check/check_degenerate_output.py" \
  "${scope_args[@]}" --missing-artifacts critical --scope vllm || exit $?

python "${TT_MODEL_BRINGUP_ROOT}/scripts/check_context_contract.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" \
  --stage vllm --require-contract || exit $?

python "${TT_MODEL_BRINGUP_ROOT}/scripts/check_serving_contract.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" --require-contract
contract_rc=$?
[ "$contract_rc" -gt 1 ] && exit "$contract_rc"

python "${TT_MODEL_BRINGUP_ROOT}/scripts/check_prefill_features.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" --stage vllm || exit $?

model_dir="${MODEL_DIR:-}"
if [ -z "$model_dir" ] && [ -n "${HF_MODEL:-}" ]; then
  model_dir="models/autoports/$(printf '%s' "$HF_MODEL" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]\{1,\}/_/g; s/^_//; s/_$//')"
fi
python "${TT_MODEL_BRINGUP_ROOT}/scripts/check_stack_freshness.py" \
  --mode verify --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" \
  --serving-contract "${model_dir}/doc/full_model/serving_contract.json"
freshness_rc=$?
[ "$freshness_rc" -gt 1 ] && exit "$freshness_rc"
[ "$freshness_rc" -gt "$contract_rc" ] && contract_rc=$freshness_rc
exit "$contract_rc"
