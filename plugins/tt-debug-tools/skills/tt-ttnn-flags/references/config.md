# Configuring TTNN

Three routes into `ttnn.CONFIG`, and they do not behave the same on a mistake.

## `TTNN_CONFIG_OVERRIDES` — a JSON string

```bash
TTNN_CONFIG_OVERRIDES='{"enable_fast_runtime_mode": false, "enable_logging": true}' pytest <test>
```

Parsed as JSON, then applied key by key. **An unknown key raises**:

```
ValueError: Unknown configuration key: <key>
```

That makes this the route to prefer: a typo fails loudly.

## `TTNN_CONFIG_PATH` — a JSON file

```bash
TTNN_CONFIG_PATH=~/ttnn-debug.json pytest <test>
```

Two behaviours worth knowing:

- If the file **does not exist**, it is created and populated with the current
  defaults. Pointing at a missing path silently gives you a defaults file, which
  then reads like something you configured.
- An unknown key only **warns**:
  `Unknown configuration key: <key>. Please update your configuration file`.
  The run continues with that setting absent, so a typo is a mode that never
  turns on and never says so.

Both are applied at import time — the file first, then the environment overrides
on top.

## `ttnn.manage_config` — scoped, in Python

```python
with ttnn.manage_config("enable_comparison_mode", True):
    ...   # only here
```

Restores the previous value on exit. The right tool for narrowing an expensive
mode to one op instead of a whole test.

## The full key set

Defaults as observed on tt-metal at the pinned ref:

| Key | Default |
|---|---|
| `enable_fast_runtime_mode` | `true` — **the gate** |
| `enable_logging` | `false` |
| `enable_graph_report` | `false` |
| `enable_graph_python_stack_traces` | `false` |
| `enable_detailed_buffer_report` | `false` |
| `enable_detailed_tensor_report` | `false` |
| `enable_comparison_mode` | `false` |
| `comparison_mode_should_raise_exception` | `false` |
| `comparison_mode_pcc` | `0.9999` |
| `throw_exception_on_fallback` | `false` |
| `enable_model_cache` | `false` |
| `root_report_path` | `generated/ttnn/reports` |
| `report_name` | `None` |
| `report_path` | `None` |
| `cache_path` | `~/.cache/ttnn` |
| `model_cache_path` | `~/.cache/ttnn/models` |
| `tmp_dir` | `/tmp/ttnn` |

`ttnn.CONFIG` prints itself, so the fastest way to confirm what a run will use is
to print it rather than to reason about precedence:

```bash
python -c "import ttnn; print(ttnn.CONFIG)"
```

## Two adjacent switches worth knowing

`throw_exception_on_fallback` is not a debug mode but belongs in the same
conversation: TTNN can fall back to a host implementation, which is correct and
slow, and silent. If an op is mysteriously slow rather than wrong, turning this
on says whether it ran on device at all.

`enable_model_cache` interacts with everything here: a cached model is not
re-executed, so per-op comparison has nothing to compare. Turn it off when
debugging, or the second run reports nothing.
