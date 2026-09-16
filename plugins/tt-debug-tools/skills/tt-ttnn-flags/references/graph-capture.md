# Graph capture

Records what TTNN did, on the host, as a queryable structure rather than a log.
Its distinctive use is naming the op that hangs: it does not touch the device, so
it works when device-side inspection is what you are trying to avoid.

Requires `enable_fast_runtime_mode: false` like everything else here.

## Capturing

```python
ttnn.graph.begin_graph_capture()
...                                    # run the model
graph = ttnn.graph.end_graph_capture()
```

| Function | Use |
|---|---|
| `begin_graph_capture()` / `end_graph_capture()` | The explicit pair |
| `end_graph_capture_to_file(path)` | End and write it out |
| `full_graph_capture(...)` | Capture around a callable |
| `is_graph_capture_active()` | Whether a capture is running — worth checking before assuming one is |

## Querying

The point of a structure over a log: ask questions rather than grep.

| Function | Answers |
|---|---|
| `extract_calltrace` | What was called, in order |
| `extract_levelized_graph` | The graph by level, for the shape of the model |
| `extract_operation_durations` | Per-op durations |
| `extract_total_duration_from_graph` | The whole capture |
| `extract_peak_L1_memory_usage` | Peak L1 — the number an L1-OOM investigation wants |
| `extract_resource_usage_per_core` | Per-core resource use |
| `extract_output_info` / `extract_output_tensors` | What came out |
| `count_intermediate_and_output_tensors` | How many tensors the graph produced |

Plus presentation: `pretty_print`, `pretty_format`, `visualize`, `graphviz`.

## Finding the hanging op

The signature is an **orphan**: a `function_start` with no matching
`function_end`. Walk the calltrace and the last unmatched start is the op that
never returned.

That works because capture is host-side and records the call boundary, so a
device-side hang leaves the start recorded and the end never written. Nothing on
the device has to be readable for this to answer.

Pair it with a bounded host wait so the process gives up and you get the capture
at all. Without a timeout the process sits in the hang and the capture is never
ended.

## Optional detail, separately switchable

These are toggled by their own functions rather than only by config, so they can
be narrowed to a region of interest:

| Pair | Adds |
|---|---|
| `enable_python_stack_traces()` / `disable_python_stack_traces()` | Python stack traces on nodes |
| `enable_detailed_buffer_tracing()` / `disable_detailed_buffer_tracing()` | Buffer detail |
| `enable_python_io_recording()` / `disable_python_io_recording()` | Python-level IO |

Each has an `is_..._enabled()` query. Stack traces in particular are expensive
and are what make a capture readable — turn them on for the region you care about
rather than the whole model.

## Comparison records

Graph capture and comparison mode share machinery:
`record_tensor_comparison_data`, `has_comparison_records`,
`flush_comparison_records_to_db`, `reset_comparison_records_data`, and a
`COMPARISON_RECORDS_SIDECAR_SUFFIX` for the sidecar file.

So a single instrumented run can answer both questions — which op diverged and
what the graph around it was — without running the model twice.

## Traps

**Capture is not free and not scoped by default.** `full_graph_capture` around
the region of interest beats a begin/end pair around a whole test.

**An unended capture yields nothing.** If the process dies inside the region,
`end_graph_capture` never runs. `end_graph_capture_to_file` narrows the window;
a host-side timeout closes it.

**Durations here are host-side.** `extract_operation_durations` measures the call,
not the device zone. For device time, `tt-profiler`.
