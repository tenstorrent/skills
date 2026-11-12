---
"tt-model-bringup": minor
---

Add goal 12, agentic evals, with the `agentic-serving` skill and its gate. Stages 1-11 work
against a configured batch set (default 1, 8, 16, 32) and record the target agent workload, a
memory ledger sized from free DRAM, and serving checks on agent-shaped load: conversations growing
to the working set (stages 9-10), prefill by prompt length and decode under host CPU load (stage
10). Optimisation work follows one measurement method (same-image control, predictions, cumulative
arms, replay-judged speed, exactness gates). Stage 11 is benchmarks only and writes
`serving_rates.json`: a batch by input-length grid including long context, plus the release rows on
the eval route. Goal 12 runs on one local machine in modes `quick` (GPQA Diamond 10, hours 0-12), `agentic`
(plus SWE-bench Verified 10, from phase 1's pass or hour 12 to hour 24) and `extended` (plus the
largest lists that fit about 10 h). A phase passes when its score is within the band of the
reference and its wall time is explained by the serving rates; one not passed when its time ends is
reported as it stands. Every failed task gets a cause, tasks cut off by a limit are rerun with
thinking and reasoning-effort settings, no list is voided, and limits come from the harness
reference. The gate is critical for missing records or analysis and rule breaks, advisory for a
phase not passed yet. `scripts/agent_replay.py` replays recorded agent histories to test capacity and caching.
Benchmark, multichip and serving-stack guidance gains slow-run diagnosis, a teardown rule and a
no-mixed-runtimes rule.
