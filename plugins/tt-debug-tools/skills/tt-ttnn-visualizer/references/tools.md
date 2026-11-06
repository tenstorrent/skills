# The MCP tools, by question

All read-only. `load_report` first: it returns the `handle` the rest take, and
`answerable` / `unanswerable` lists that save probing tool by tool.

ttnn-visualizer v0.110.0, the minimum this skill supports, ships every tool
below. A report can still leave one unanswerable — a truncated capture
missing the tables a tool reads, say. Then use the nearest one `load_report` lists
(`memory_profile` plus `operation_detail` for L1) and say the answer is weaker
for it.

`load_report` takes the memory report folder as `memory_report_path`. Its
answer still names it `profiler_path`, and `device_count.profiler` counts the
memory report's devices.

## Database tools (`memory_report_path`, `db.sqlite`)

| Tool | Answers | Watch for |
|---|---|---|
| `errors` | What went wrong, in order; allocation failures with their figures and a diagnosis (no free space, fragmented, larger than an empty bank, CBs past end of L1). | `incomplete_operation` = started, never finished — where it crashed. |
| `l1_peak` | The L1 peak per bank and its make-up: CBs, intra-op, persistent and stale tensors. | Only stale tensors are fixable by freeing earlier. First call replays every graph — seconds on big captures. |
| `memory_profile` | Footprint per op, by buffer type, plus `l1_pressure`. | A **floor**, not the peak. |
| `dram_fallbacks` | Ops that likely fell back from L1 to DRAM. | Inferred, not recorded by tt-metal. A lead, not a fact. |
| `operation_detail` | One op: tensors with memory config, allocations, circular buffers, error, `l1_pressure`. | CBs are recorded only at a program's first launch; read `unrecorded_launch_count`. |
| `find_operations` | Ops by name or by `called_from` stack substring. | Host durations, not device time. |
| `operation_provenance` | Arguments and the model-code call site. | Needs `enable_graph_python_stack_traces`. |
| `tensor_flow` | Producer and consumers of a tensor. | |
| `subgraph_between` | Every op and edge between two ops. | |

## Performance tools (`performance_path`, CSV)

| Tool | Answers | Watch for |
|---|---|---|
| `top_ops` | Costliest ops by `device_time`, `op_to_op_gap`, `total_percent`, `flops`, `dram`, `cores`. `include` adds why: `config`, `utilisation`, `riscs`, `cores`, `noc`, `dispatch`, `cache`, `advice`, `flags`. | A null `bound` is not healthy — read `bound_analysis`. |
| `op_breakdown` | Time per op or per category. | |
| `program_cache` | Repeat calls that rebuilt their program. | Needs a run that repeats ops. |
| `core_spread` | Per-core imbalance inside an op. | |
| `zone_timings` | Per-zone, per-RISC device totals. | |
| `zone_intervals` | Start-to-start time of a zone that recurs within one kernel run — per loop iteration, the steady-state time: median, min and max per device and per RISC. | Listed only when the device log marks zone starts and ends and names its chip frequency; never pooled across devices. Flags runs that filled the profiler's L1 buffer. |
| `diff_reports` | Per-op-code change between two captures, above a noise threshold. | Pass `repeats_a` / `repeats_b` for medians; one capture each side is anecdote. |

`top_ops`, `op_breakdown` and `diff_reports` take `start_signpost` /
`end_signpost`, read the way `tt-perf-report --start-signpost` reads them.

## Both

`critical_path` — longest chain of device time through the op graph. Needs
linked reports.

## Units

Device time and op-to-op gap are microseconds; an operation's `duration` is
host seconds. The id-space, per-bank and `app_link` rules are SKILL.md's Traps.

## Reading answers

Read `projection`, `note` and `caveat` before quoting a number. Limits are
capped server-side; asking for more returns the cap. Fill
`app_link_template` only with an integer `operation_id` from a tool answer.
