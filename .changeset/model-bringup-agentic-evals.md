---
"tt-model-bringup": minor
---

Add goal 12, agentic evals, with the `agentic-serving` skill and its gate. Stages 1-11 work
against a configured batch set (default 1, 8, 16, 32) and record the target agent workload, a
memory ledger sized from free DRAM, and serving checks on agent-shaped load: conversations growing
to the working set (stages 9-10), prefill by prompt length and decode under host CPU load (stage
10). Goal 12 runs on the local device by default, in `light` (24 h) or `full` (48 h) mode: a
survey and preflight, then growing task lists on the harness as it is on main (smoke, nightly
lists, a server-filling run), efforts ranked validity first, and a rehearsed release run that
starts inside the time limit and fits `run_timeout_hours`. Extra devices are optional and defined
by the operator. `scripts/agent_replay.py` replays recorded agent histories to test capacity and
caching. Benchmark, multichip and serving-stack guidance gains slow-run diagnosis, a teardown
rule and a no-mixed-runtimes rule.
