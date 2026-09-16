---
name: tt-ttnn-flags
description: Turn on TTNN's host-side debug modes — graph capture to find the op that hangs, per-op golden comparison to find the op that diverges, and the buffer and tensor reports. Use when a TTNN model produces wrong output and you need the first op that goes wrong, or when a model hangs and you want the op named from the host without touching the device. Every one of these is silently inert while fast runtime mode is on.
metadata:
  tier: op
  upstream:
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: ttnn/ttnn/__init__.py
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: ttnn/ttnn/graph.py
---

# tt-ttnn-flags

Host-side, TTNN-level, and entirely off by default. Two answer adjacent
questions:

- **Graph capture** names the op that *hangs* — an orphan `function_start` with
  no `function_end`.
- **Comparison mode** names the op that *diverges* — each op checked against a
  golden reference as it runs.

Both are gated behind the same switch, and that switch defaults to the wrong
value for debugging.

## When to invoke

- A TTNN model hangs and you want the op named without reading device state.
- A model's output is wrong and you need the *first* op that diverges rather
  than the last one you looked at.
- You want per-op buffer or tensor reports.

Not this skill: device-side state — `tt-triage`, `tt-watcher`. Kernel-level
values — `tt-dprint`.

## Surface

`enable_fast_runtime_mode` is **`True` by default**, and it disables the debug
modes. Turning a mode on without turning that off does nothing at all — no
error, no output.

```bash
TTNN_CONFIG_OVERRIDES='{"enable_fast_runtime_mode": false, "enable_comparison_mode": true}' pytest <test>
```

| Key | Default | Effect |
|---|---|---|
| `enable_fast_runtime_mode` | **`true`** | Set `false` first; everything below is inert otherwise. |
| `enable_comparison_mode` | `false` | Compare each op against a golden reference. |
| `comparison_mode_pcc` | `0.9999` | The threshold a comparison must meet. |
| `comparison_mode_should_raise_exception` | `false` | Stop at the first failing op rather than logging it. |
| `enable_logging` | `false` | Per-op logging; the base for the reports. |
| `enable_graph_report` | `false` | Emit the captured graph. |
| `enable_graph_python_stack_traces` | `false` | Python stack traces on graph nodes. |
| `enable_detailed_buffer_report` | `false` | Per-op buffer detail. |
| `enable_detailed_tensor_report` | `false` | Per-op tensor detail. |
| `throw_exception_on_fallback` | `false` | Fail rather than silently falling back to a host implementation. |
| `root_report_path` | `generated/ttnn/reports` | Where reports land. |
| `report_name` | `None` | Names this report. |

Two ways in — `TTNN_CONFIG_OVERRIDES` as a JSON string, or `TTNN_CONFIG_PATH`
pointing at a JSON file. Plus `ttnn.manage_config(name, value)` as a context
manager to scope a change. Details and the graph API:
`references/config.md`, `references/graph-capture.md`.

## Force the state

Comparison mode against any op with a golden reference:

```bash
TTNN_CONFIG_OVERRIDES='{"enable_fast_runtime_mode": false, "enable_comparison_mode": true, "comparison_mode_should_raise_exception": true}' \
  pytest <a ttnn op test>
```

## Output

Reports under `root_report_path`, default `generated/ttnn/reports`, named by
`report_name`. Comparison mode logs a PCC per op and, with
`comparison_mode_should_raise_exception`, raises at the first op below
`comparison_mode_pcc`.

Graph capture returns a structure you query rather than a file you read —
`ttnn.graph.extract_calltrace`, `extract_peak_L1_memory_usage`,
`extract_operation_durations` and friends. The hang signature is an
`incomplete_operation`: a `function_start` with no matching `function_end`.

## Traps

**Fast runtime mode is on by default and silences everything here.** This is the
single fact that matters. A run with `enable_comparison_mode: true` and fast
runtime mode left alone produces no comparisons and no complaint.

**An unknown key behaves differently by route.** From `TTNN_CONFIG_OVERRIDES` it
raises `ValueError: Unknown configuration key`. From a `TTNN_CONFIG_PATH` file it
only *warns* and carries on — so a typo in the file is a mode that never turns
on.

**`TTNN_CONFIG_PATH` writes the file if it does not exist.** Pointing at a
missing path creates it populated with defaults rather than failing, which then
looks like a config you wrote.

**Comparison mode does not stop by default.** `comparison_mode_should_raise_exception`
is `false`, so a diverging op is logged and the run continues to produce a wrong
answer. For "which op first" you want it `true`.

**PCC 0.9999 is the threshold, not a target.** It is stricter than many ops need
and looser than some do. A failure at this default is a signal to look, not proof
of a bug.

**These cost real time.** Per-op comparison runs a golden implementation for
every op. Do not read timings from a run with these on, and do not leave them in
a config file you keep using.
