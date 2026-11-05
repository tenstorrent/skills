# Goal 12 survey

Read at the start of goal 12. The survey decides where to look before any scored run. Every input
exists by the end of the benchmark stage, plus one smoke run at release limits. Record the result
in `doc/agentic_evals/survey.json`; the gate reads the keys named below.

## Checks

Read these on the stage-11 benchmark and the smoke run, and again on every later run.

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
| `cutoffs` | Share of tasks ending at the time limit or the output cap, split into still progressing and looping | Near 0 |
| `eval_validity` | Network isolation, fetched fixes, infrastructure failures | Isolated, no fetches |
| `accuracy_drift` | KL against HF on long agent prompts | At the noise floor |
| `generation_share` | Per model call on the smoke: output tokens × served step ÷ call wall, and what fills the rest (tool commands, container round trips, prefill waits) | Known; it bounds what any serving change can save |
| `prefill_under_load` | Prefilled tokens ÷ the agents' final context tokens, prefill tok/s at real context lengths, decode rate while a long prompt prefills | Prefilled close to the final contexts; decode not stalled |
| `host_contention` | Engine host time per step against task-container CPU, from a host monitor during the smoke | Flat as task CPU rises |

Record each as `{"value": ..., "healthy": true|false}`. A check you cannot measure yet records
`not_measured` with the reason and the test that will measure it.

## Calculations

Work these out before any device time. Each gives a setting or a size, not a guess to be tested by
eval.

| Key | Question | Formula |
|---|---|---|
| `batch` | Which batch for N agents? | The validated batch with the least time per call: output tokens ÷ its per-user rate + time outside generation; usually the smallest at or above N |
| `agents` | How many agents per server? | min(`max_num_seqs`, pool tokens ÷ working set per agent, host trial limit) |
| `suite_hours` | Will each suite finish? | From the nearest suite-timing prior in harness facts: tasks ÷ agents × per-task wall, tail at the agent limit; after a rung, from its measured wall. The serving formula tasks × (uncached tokens ÷ prefill tok/s + output tokens × step ÷ busy rows) ÷ agents is a cross-check, and covers only generation |
| `task_lists` | Which tasks per rung, and how many at full load? | Inputs, else the harness's lists, else the skill's default rule; full load = `agents`; 95% interval ±2·√(p(1−p)/n) |
| `agent_timeout` | What agent time limit? | turns × turn time at the served speed, never below the release limit |
| `output_cap` | What output cap? | Longest answer so far plus headroom, never below the release cap |
| `bytes` | Where do the bytes go? | Per cache ID: block × KV bytes per token + state bytes; fill per layer group; duplicate weights |
| `cache_agents` | How many agents keep the prefix cache? | agents ≤ pool IDs ÷ (live blocks + state IDs per agent) |
| `block_cost` | What does the block size cost? | Recompute per returning turn ≈ block ÷ 2 |
| `prefill_padding` | How much prefill is useful? | live rows ÷ batch rows |

`suite_hours` is the first go or stop estimate. Update it after every rung and effort, and record
the ratio of prediction to measurement.

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
| Agent replay | Recorded agent histories replayed at the target agent count with one output token per call (`scripts/agent_replay.py`): hit rate, prefilled ÷ final context tokens, time per call, preemptions | Pool too small for real contexts, agent count, block size, cache layout |
| Host monitor | CPU per process and per container every 20 s beside the engine's step timers, during the smoke | Task containers starving the engine thread |

The replay is the capacity test. Qwen's synthetic 32-agent test passed while real SWE histories
collapsed the cache: replaying 8 recorded histories on the default pool finished 0 of 8 agents in
45 minutes at 68.5% hits; on a pool sized to hold them, 8 of 8 in 37 minutes at 91.1% hits and no
preemptions. Record histories from any earlier run, including the smoke, as one JSONL file per
agent with one chat request per line.

