#!/usr/bin/env bash
TT_MODEL_BRINGUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Runner-side gate for the optimized-vLLM stage: serving optimization (async
# decode, trace reuse, on-device sampling) must not have introduced stale-input
# or sampler degeneracy into served outputs, and the eval-readiness probes for
# the stage must pass. Scoped to this run's model.
# Exit 0 pass, 1 advisory, 2 critical, 3 error.
if [ -n "${MODEL_DIR:-}" ]; then
  scope_args=(--model-dir "$MODEL_DIR")
elif [ -n "${HF_MODEL:-}" ]; then
  scope_args=(--hf-model "$HF_MODEL")
else
  echo "Neither MODEL_DIR nor HF_MODEL is set; cannot scope the check to the target model." >&2
  exit 3
fi
# Every sub-check runs; the worst code wins, and 3 (or any code above 3) is a
# checker error that stops the gate.
worst=0
gate() {
  "$@"
  local rc=$?
  if [ "$rc" -ge 3 ]; then echo "check error (exit $rc): ${2:-$1}" >&2; exit 3; fi
  if [ "$rc" -gt "$worst" ]; then worst=$rc; fi
}

gate python "${TT_MODEL_BRINGUP_ROOT}/runtime/readiness_check/check_degenerate_output.py" \
  "${scope_args[@]}" --missing-artifacts critical --scope vllm

gate python "${TT_MODEL_BRINGUP_ROOT}/scripts/check_context_contract.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" \
  --stage optimized-vllm --require-contract

# Prefix caching stays required; the batched-prefill burst ratio is advisory (D5)
# unless --burst-critical is passed. Batched-equals-solo evidence stays critical
# when the serving contract enables batched prefill.
gate python "${TT_MODEL_BRINGUP_ROOT}/scripts/check_prefill_features.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" \
  --stage optimized-vllm --require-burst

# Eval readiness: P4 long answers with 32 users + health, P5 long-context timing.
gate python "${TT_MODEL_BRINGUP_ROOT}/scripts/check_eval_readiness.py" \
  --model-dir "${MODEL_DIR:-}" --hf-model "${HF_MODEL:-}" --stage optimized-vllm
exit "$worst"
