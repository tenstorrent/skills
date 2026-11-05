# Harness facts for goal 12

Read before the survey. These were measured on the QB2 agentic route and do not depend on the
model. Check each against the run; a value that differs is a finding for `learned.json`. Do not
spend an effort re-deriving one. Each value is evidence from a named run, not a default to
select by; when the route changes, measure again. Facts are updated from each run's `learned.json` between skill
versions.

## Hosts and runners

| Fact | Value | Source |
|---|---|---|
| Concurrent Harbor trials per Docker host | At most 29. Each trial creates its own bridge network; Docker's default address pools give 31, and `bridge` plus one long-lived network take two. At 32 trials, 2–3 died before the agent started. Widening the pools needs root. `AGENTIC_MAX_CONCURRENT_TRIALS` caps it | Qwen3.8 local run, 3 Nov; Gemma 4 31B SWE-32, 4 Nov |
| Task CPU and memory | Use each task's declared limits. Terminal-Bench declares 1 CPU for 84 of 89 tasks. A blanket 16-CPU override let busy tasks starve the engine's host thread: at over 600% task CPU, decode fell to 170 steps a minute, against 471 with task containers confined to cores 4–7 | Gemma 4 31B speedup, host CPU contention |
| Declared task storage | 10 GiB per task environment; the admission gate reserves 512 GiB before a model loads. Check cohort × 10 GiB + 512 GiB against free disk at preflight | Gemma 4 31B stage 12: ENOSPC three times, five SWE tasks lost |
| Runner disk | Harbor task images and volumes are not reclaimed. A 6.6 h agentic run filled a 3.9 TB runner, and one runner sat at 94% from accumulated images. Prune between trials; record free space before and after each run | Qwen3.8 follow-up (p05t01); Gemma follow-up (p05t06) |
| Runner choice | Jobs target the pool label (`bh-qb-ge`); per-host labels do not exist. The scheduler favours the most recently idle machine, so a failed job's retry can land on the same broken runner. Re-dispatch infrastructure failures; never score them | Gemma 4 31B follow-up |
| Long agentic jobs | The 18 h job limit holds a 32-task suite at 8–29 agents; split larger suites on task boundaries | Qwen3.8 and Gemma follow-ups |

## Repositories and harness

| Fact | Value | Source |
|---|---|---|
| Config files in tt-inference-server | `*.json` is gitignored. Force-add new config files; a precision config that never reached CI made the server fall back silently. Confirm the loaded config in the server log | Qwen3.8 follow-up |
| Suite selection on CI | Main's CI mode runs each model's `ci-nightly` lists from `reference_config/evals/eval_config.py`. To run one suite per runner, set the other suites' list limit to 0 in the dispatched commit. The tt-shield dispatch also exposes `agentic-benchmark` (a non-overlapping partition; empty runs all suites) | Qwen3.8 and Gemma follow-ups |
| SWE-bench Verified task list | `llm_module/agentic/swe500_registry.json` in tt-inference-server: the sorted 500 at the pinned dataset commit | Gemma 4 31B follow-up |
| SWE agent environment | Agent PATH `/opt/miniconda3/envs/testbed/bin:…`, as the retained `harbor_config.json` records | Granite 4.2 release run |
| Tool calling | mini-swe-agent and terminus need the release server's tool-call parser and tool-choice flags. A local launcher copies the release server flags | Gemma and Qwen stage 12 |
| Local server reachability | A sandboxed agent cannot reach a server published only on the host's loopback. Harbor's default bridge needs a relay to the server; the model launcher's readiness check still uses `127.0.0.1:8000` | Qwen3.8 stage 12 and follow-up |
| Verifiers offline | Some verifiers install packages (`pytest==8.4.1`) when they start; with egress denied they fail and the score is invalid. The stock route does not deny egress; the trajectory audit catches fetched fixes | Gemma 4 31B stage 12 |
| References | Gemma 4 31B has no `swe_bench_verified` reference in its config block, so SWE grades NA however well it is measured. Check every suite the release depends on has one | Gemma 4 31B follow-up |

## Suite timings

Seed priors for `suite_hours`. Use the nearest row: tasks ÷ agents × per-task wall, with the tail
at the agent limit. After each rung, replace the prior with the measured wall and record the ratio
of prediction to measurement. In stage 12, suite forecasts were 1.9–4.9× too high on Gemma and
3–4× too low on Qwen; no fixed margin corrects both, one measured rung does.

| Model | Eval | Tasks | Agents | Batch | Wall h | Agent h | Score | Cut at limit | Notes |
|---|---|---|---|---|---|---|---|---|---|
| Gemma 4 31B | SWE-bench Verified | 32, stratified | 29 | 32 | 5.17 | 36.2 | 15/32 | 3 at 300 min | CI 41805078493 |
| Gemma 4 31B | SWE-bench Verified | 32, stratified | 10 | 8 | 4.75 | 27.1 | 17/32 | 0 | CI 41809368690; generation 19% of agent time |
| Gemma 4 31B | Terminal-Bench 2 | 32 | — | 8 | — | — | 12/32 | 4 at 300 min | stock route, ratio 0.953 to reference |
| Gemma 4 31B | SWE-bench Verified | 500 | 40 | — | 5.26 | — | 299/500 | — | tuned serving, outside the stage |
| Gemma 4 31B | Terminal-Bench 2 | 89 | 40 | — | 3.0 | — | 35–39/89 | at the 3 h agent limit | tuned serving, outside the stage |
| Qwen3.8-27B | Terminal-Bench 2.1 | 10 | 10 | 8 | 2.9 | — | 8/10 | 0 | CI 41805491132 |
| Qwen3.8-27B | SWE-bench Verified | 10 | 10 | 8 | 6.6 | — | 7/10 | 8 at 6 h | CI 41805496861; pool-bound, prefilled 24× the final contexts |
| Qwen3.8-27B | Terminal-Bench 2.1 | 32 | 29 | 32 | 6.0 | — | 16/32 | 8 at 6 h | local |
| Qwen3.8-27B | SWE-bench Verified | 32 | 29 | 32 | 7.1 | — | 2/32 | 29 at 6 h | local; pool held 8 histories |
| Granite 4.2-30B | Terminal-Bench | 10 | 5 | — | — | 16.74 | 4/10 | 1 at 360 min | release run |
| Gemma 4 26B-A4B | SWE-bench Verified | 5 | 1 | — | — | 1.83 | 2/5 | 0 | 18–26 min per task |
| Gemma 4 26B-A4B | Terminal-Bench | 5 retained | — | — | — | 9.98 | — | 3 at 180 min | release run |
