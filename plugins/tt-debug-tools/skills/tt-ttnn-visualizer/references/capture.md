# Capturing reports the visualizer can read

Two independent captures. Each answers a different set of tools; `load_report`
says which.

## Memory report (`db.sqlite`)

Written by tt-metal's root `conftest.py`: an autouse pytest fixture, active only
when `enable_logging` is on, begins graph capture before the test and, at
teardown, imports it into `db.sqlite`. So this route works **only under
pytest**, and only once the test has finished or raised. Comparison mode
without `enable_graph_report` also writes a `db.sqlite`, but holding comparison
records only — no graph, so most tools have nothing to read.

The command is the one in SKILL.md under "Force the state". Why each key:

| Key | Why |
|---|---|
| `enable_fast_runtime_mode: false` | Everything else here is inert while it is on (`tt-ttnn-flags`). |
| `enable_logging: true` | The fixture does nothing without it. |
| `enable_graph_report: true` | Starts the graph capture the database is built from. |
| `report_name` | Names the folder. Set it explicitly: older tt-metal skips the report when it is unset, newer derives one from the test id. |
| `enable_detailed_buffer_report: true` | Per-op buffer detail — `l1_peak`, `memory_profile`, `operation_detail` allocations. |
| `enable_graph_python_stack_traces` | `true` only for call sites — `operation_provenance`, `find_operations(called_from=...)`. Costs host time per op. |
| `enable_detailed_tensor_report: false` | Tensor values; the visualizer does not use them and they are large. |

Every missing condition fails silently: no report, no error, often no folder.

Output: the newest folder by mtime under
`$TT_METAL_HOME/generated/ttnn/reports/` — name is `report_name` plus a date
suffix — holding `db.sqlite` and usually `config.json`. Pass that folder as
`profiler_path`.

Outside pytest (a demo script, a server) this fixture does not run. The graph
API in `tt-ttnn-flags` (`references/graph-capture.md`) is the starting point;
follow tt-metal's current graph-tracing docs for importing the capture into a
report rather than guessing.

## Performance report (CSV)

`tt-profiler`'s Tracy recipe. Its output folder,
`$TT_METAL_HOME/generated/profiler/reports/<timestamp>/`, holding
`ops_perf_results_<timestamp>.csv` and `profile_log_device.csv`, is the
`performance_path`.

## Linking the two

`load_report` takes both paths. Linked — same run only, see the Traps — `top_ops`
rows gain an `operation_id`, and `dram_fallbacks`, `critical_path` and the perf
fields of `operation_detail` light up. Take headline timings from a separate
profiler-only run.

## Multi-host

Each rank writes its own data. Database tools read rank 0 unless given `rank`,
and ids restart per rank, so never mix ranks in one question.
