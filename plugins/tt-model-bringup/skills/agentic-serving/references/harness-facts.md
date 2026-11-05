# Harness facts for goal 12

Read before the survey. These were learned on the QB2 agentic route and do not depend on the
model. Check each against the run and record any that differ in `learned.json`. Each value is
evidence from a named run, not a default; measure again when the route changes.

| Fact | What was seen | Source |
|---|---|---|
| Concurrent trials per Docker host | Each Harbor trial creates its own bridge network and the default address pools give about 31; at 32 trials some died before the agent started, at 29 none did | Qwen3.8 local run; Gemma 4 31B SWE-32 |
| Task CPU and memory | With every task allowed all host CPUs, busy tasks starved the engine's host thread and decode slowed many times over; confining tasks away from the engine's cores removed most of it. Use the limits the tasks declare | Gemma 4 31B local speedup work |
| Disk | Task images and volumes are not reclaimed; a long agentic run filled a runner's disk. Check cohort × declared task storage against free space before a run, and prune between runs | Qwen3.8 follow-up; Gemma 4 31B stage 12 |
| Config files in tt-inference-server | `*.json` is gitignored, so a new config file never reached CI and the server fell back silently. Force-add it and confirm it loaded in the server log | Qwen3.8 follow-up |
| Local server reachability | A sandboxed agent cannot reach a server bound only to the host's loopback; it needs a relay. The launcher's readiness check still uses loopback | Qwen3.8 follow-up |
| Agents against the pool | When agents' histories outgrow the KV pool, the prefix cache evicts them and each call reprocesses most of its history; replaying real histories shows this in minutes, short synthetic tests do not | Qwen3.8 follow-up |
| Time outside generation | Most of an agent's wall time can be tool work and container round trips, so batch and decode speed-ups save less than per-user rates suggest | Gemma 4 31B SWE-32 pair |

## Suite timings

Seed priors for `suite_hours`. Use the nearest row; after the first full-load run, use its measured
time instead.

| Model | Eval | Tasks | Agents | Batch | Wall h | Score | Cut at limit |
|---|---|---|---|---|---|---|---|
| Gemma 4 31B | SWE-bench Verified | 32 | 29 | 32 | 5.17 | 15/32 | 3 |
| Gemma 4 31B | SWE-bench Verified | 32 | 10 | 8 | 4.75 | 17/32 | 0 |
| Gemma 4 31B | SWE-bench Verified | 500 | 40 | — | 5.26 | 299/500 | — |
| Gemma 4 31B | Terminal-Bench 2 | 89 | 40 | — | 3.0 | 35–39/89 | at the 3 h agent limit |
| Qwen3.8-27B | Terminal-Bench 2.1 | 10 | 10 | 8 | 2.9 | 8/10 | 0 |
| Qwen3.8-27B | SWE-bench Verified | 10 | 10 | 8 | 6.6 | 7/10 | 8, pool-bound |
| Qwen3.8-27B | Terminal-Bench 2.1 | 32 | 29 | 32 | 6.0 | 16/32 | 8 |
| Qwen3.8-27B | SWE-bench Verified | 32 | 29 | 32 | 7.1 | 2/32 | 29, pool-bound |
