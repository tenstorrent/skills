# Goal 12 survey

Read at the start of goal 12. The survey decides where to look before any scored run. Every input
exists by the end of the benchmark stage, plus one smoke run at release limits. Record the result
in `doc/agentic_evals/survey.json`; the gate reads the keys named below.

## Checks

Read these on the stage-11 benchmark and the smoke run, and again on every later run. Each answered
a pivotal question on an earlier bringup, and each failed at least once.

| Key | How to measure | Healthy |
|---|---|---|
| `time_split` | Prefill, decode, idle, trace capture and startup as shares of engine time | Decode dominates |
| `busy_rows` | Mean active rows ÷ batch rows per decode step | Close to 1 |
| `live_prefill_rows` | Real rows ÷ batch rows in each prefill call | Close to 1 |
| `trace_captures` | Decode trace captures per request after warm-up | 0 |
| `served_step` | Served decode step against the benchmarked step | Within a few % |
| `prefix_hits` | Cached ÷ prompt tokens on returning turns | 0.9 or more |
| `cache_pressure` | Peak pool use, preemptions, evictions | Close to 0 |
| `cache_bytes` | Bytes per cache block by layer group, and how full each is | Little idle |
| `memory_headroom` | Minimum free DRAM under load, and weights held twice | Pool sized from the minimum |
| `cutoffs` | Share of tasks ending at the time limit or the output cap | Near 0 |
| `eval_validity` | Network isolation, fetched fixes, infrastructure failures | Isolated, no fetches |
| `accuracy_drift` | KL against HF on long agent prompts | At the noise floor |

Record each as `{"value": ..., "healthy": true|false}`. A check you cannot measure yet records
`not_measured` with the reason and the test that will measure it.

## Calculations

Work these out before any device time. Each gives a setting or a size, not a guess to be tested by
eval.

| Key | Question | Formula |
|---|---|---|
| `batch` | Which batch serves the planned clients? | Smallest validated batch ≥ planned clients |
| `suite_hours` | Will each suite finish? | tasks × (uncached tokens ÷ prefill tok/s + output tokens × step ÷ busy rows) ÷ agents |
| `agent_timeout` | What agent time limit? | turns × turn time × margin, never below the release limit |
| `output_cap` | What output cap? | Raise it while any answer hits it |
| `bytes` | Where do the bytes go? | Per cache ID: block × KV bytes per token + state bytes; fill per layer group; duplicate weights |
| `agents` | How many agents keep the prefix cache? | agents ≤ pool IDs ÷ (live blocks + state IDs per agent) |
| `block_cost` | What does the block size cost? | Recompute per returning turn ≈ block ÷ 2 |
| `prefill_padding` | How much prefill is useful? | live rows ÷ batch rows |

`suite_hours` is the first go or stop estimate. Update it after every effort.

## Shortfalls carried in

Earlier stages may pass with a recorded reason: a second weight copy, a KV pool below concurrency ×
working set, prefix caching off or below 0.9 of the ceiling, traces rebuilt after warm-up. List
each under `shortfalls` with the hours it costs the suites, from the calculations above. A
shortfall that costs hours is a problem to rank, not a footnote.

## Short tests

Most problems the checks raise are found and proven by a test of minutes, not a scored eval.

| Test | What it measures | Finds |
|---|---|---|
| Preflight | Registry, disk, release route, isolated 2-task smoke with a trajectory audit | Pull and permission failures, network leaks |
| Op profile at the target batch | Time per op and per layer type, ranked | Slow kernels at the serving batch |
| Per-layer split at batch 1 | Non-matmul ops ranked by share | Reshape and layout overhead |
| Paired exactness check | Tokens, recurrent state and logits, candidate against baseline | Accepts a speedup without an eval |
| Long-prefix KL against HF | Divergence on real agent prompts, per precision | Precision that breaks tool calls |
| Prefill vs batch shape | One live row at batch 1, 8 and the serving batch | Padded prefill |
| Memory under stress | Allocator events and minimum free DRAM over a loaded run | Safe pool size |
| Cache replay | Recorded traffic through the prefix cache, per layout and agent count | Agent count, block size, cache layout |

## Ranking

Each problem becomes one effort in `efforts.json` with the evidence from the checks, the saving
from the calculations (suite hours per eval), the hours of work, and how the effort will confirm it
worked, usually one of the short tests above or a calculation. A problem the calculations already
answer, such as batch size or limits, is confirmed by the setting itself. Rough estimates are
fine; mark guesses. The goal-12 section of the skill gives the order and when to re-rank.
