#!/usr/bin/env bash
TT_MODEL_BRINGUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"
# Runner-side gate for the agentic-evals stage: serving invariants still hold (autoport entry);
# each phase of the mode has a collected run with its records and per-task analysis, no list is
# voided, and a task cut off by a limit shows a loop or stall or was rerun at a higher limit
# (critical otherwise); each run's KV pool and agents are sized from the device memory measured free at
# launch (critical without a reason when the pool is below half of it); each degenerate reply (anomaly) and
# phase below its band has an anomalies entry with a reproducible test and its full-precision result, and a
# precision fix re-measures its rate rows and reruns the list at the fastest passing config tried (critical
# otherwise); the logging check before the first eval run passed, every scored phase run has a reply digest
# (reply_digest.py) with a row for each failed task, and a failed task with a flagged reply has cause anomaly or
# a recorded reason (critical otherwise). Advisory: a phase not passed yet (score below its band, time the
# stage-11 rates do not explain, with a reason), recomputed marks that differ from the record, unmapped or
# flagged replies on solved tasks, digests of other runs, queueing beyond max_num_seqs. A checker crash is critical.
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
# Stage usage: the runner writes `## Stage usage` and stage_usage.json before this check (2 if missing or malformed).
"$PYTHON" "${TT_MODEL_BRINGUP_ROOT}/scripts/stage_usage.py" check --model-dir "${MODEL_DIR:-}" --stage 12-agentic-evals || usage=$?
[ "${usage:-0}" -gt "$rc" ] && rc=$usage
exit "$rc"
