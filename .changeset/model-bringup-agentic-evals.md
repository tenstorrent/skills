---
"tt-model-bringup": minor
---

Add goal 12, agentic evals, with the `agentic-serving` skill and its gate. Stages 1-11 work
against a configured batch set (default 1, 8, 16, 32) and record the target agent workload, a
memory ledger sized from free DRAM, and serving checks on agent-shaped load: conversations growing
to the working set (stages 9-10), prefill by prompt length and decode under host CPU load (stage
10). Optimisation work follows one measurement method (same-image control, predictions, cumulative
arms, replay-judged speed, exactness gates). Stage 11 is benchmarks only and writes
`serving_rates.json`: a batch by input-length grid up to the longest context the evals reach, also at
request concurrency 1, plus the release rows on the eval route, and a long sampled-generation
anomaly check. Goal 12 runs on one local machine in modes `quick` (GPQA Diamond 10), `agentic` (plus
SWE-bench Verified 10, once phase 1 has passed or is analysed, at the latest at `phase1_hours`) and
`extended` (plus the largest lists that fit about 10 h). A phase passes when its score is within the
band of the reference and its wall time is explained by the serving rates (`scripts/explain_hours.py`);
one not passed when its time ends is reported as it stands. Every failed task gets a cause from a
reply digest of the server's response log (`scripts/reply_digest.py`) and a triage sub-agent; a task
cut off while progressing is rerun at a higher limit, failed tasks rerun together as waves, and a
degenerate reply or a phase below its band gets a precision fix attempt (`datatype-sweep`). Runs start
detached and are waited on without model calls (`scripts/eval_runs.py`). The gate is critical for
missing records or analysis and rule breaks, advisory for a phase not passed yet. The multigoal runner
writes each stage's Codex tokens (by thread family) and device held/idle hours into its report.
`scripts/agent_replay.py` replays recorded agent histories to test capacity and caching. Benchmark,
multichip and serving-stack guidance gains slow-run diagnosis, a teardown rule and a no-mixed-runtimes rule.
