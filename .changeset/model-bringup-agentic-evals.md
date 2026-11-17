---
"tt-model-bringup": minor
---

Add goal 12, agentic evals, and the `agentic-serving` skill that stages 6-12 load. Stages 1-11
work against a configurable batch set (`batch_sizes` in `bringup/agentic_evals/inputs.json`,
default 1, 8, 16, 32) and record the target agent workload, a memory ledger sized from free DRAM
and serving checks on agent-shaped load. Stage 11 is benchmarks only: it writes `serving_rates.json`
(batch by input length, also at request concurrency 1). Goal 12 runs GPQA Diamond and SWE-bench
Verified lists (modes `quick`, `agentic`, `extended`) and checks that the score is within a band of
its reference and that the wall time is explained by those rates; a phase not passed when its time
ends is reported as it stands. Every failed task gets a cause, and a degenerate reply or a phase
below its band gets a precision-fix attempt (`datatype-sweep`). Optional helper scripts
(`eval_runs.py`, `reply_digest.py`, `explain_hours.py`) run evals detached, digest replies and
compute explained time; the gate reads only the agent's records and blocks only where a score or a
failure could be hidden. The runner refuses to start stage 12 over a non-empty `doc/agentic_evals` (`--keep-stage-outputs`
overrides).
