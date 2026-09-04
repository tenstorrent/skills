# Fixtures — real tool output from a real device

A skill is proven by two different tests, and they need different things.

**Usage tests** (`test_<skill>_usage.py`) ask an agent how to drive the tool.
They catch invented environment variables and flags, wrong gtest filters, and
answers that contradict a documented constraint. No device. Cheap. They do not
prove the tool works or that the agent can read what it produces.

**Interpretation tests** (`test_<skill>_reading.py`) hand the agent output
captured from a real run and ask what happened. This is the test that matters.
It needs no device *at test time* — the output is a committed file — but the
file has to be captured on hardware first.

## Layout

One directory per scenario:

```
fixtures/<skill>/<scenario>/
  cmd.sh          the exact command that produced this, runnable
  output.txt      what the tool actually printed or wrote
  meta.json       arch, board, tt-metal commit, capture date
  expected.json   the correct reading — authored by a human from output.txt
```

`expected.json` holds only the fields the scenario actually pins:

```json
{
  "primary_signal": "check_core_magic",
  "verdict": "no",
  "evidence_strength": "weak",
  "must_contain": ["NCRISC", "cb_wait_front"]
}
```

A test with no fixture directory **skips**. It does not pass. An absent fixture
is missing evidence, and a green suite that proves nothing is worse than a
skipped one.

## Capturing

`tests/capture_fixtures.sh` runs the provoking programs and writes the
directories. It needs a Tenstorrent device, a built tt-metal, and
`TT_METAL_HOME` set. Run it from the tt-metal checkout:

```bash
TT_METAL_HOME=/path/to/tt-metal \
  plugins/tt-debug-tools/tests/capture_fixtures.sh /path/to/fixtures
```

Then read each `output.txt` and author `expected.json` by hand. That authoring
is the ground truth — do not generate it with the same agent the test grades, or
the test measures agreement with itself rather than correctness.

## Provoking programs

Each scenario is a concrete program that puts the device in the state the tool
reports on. Upstream already ships most of them as gtest cases whose whole
purpose is to force a specific fault.

| Skill | Scenario | Provoking program |
|---|---|---|
| `tt-noc-dump` | `missing-write-barrier` | `unit_tests_noc_debugging --gtest_filter=NOCDebuggingFixture.McastOnlyWriteFlush` — multicast write, semaphore increment, no barrier |
| `tt-triage` | `halted-core` | `unit_tests_debug_tools --gtest_filter=MeshWatcherFixture.TensixTestWatcherSanitize` — a bad NoC write halts a core, then triage reads it |
| `tt-triage` | `healthy-run` | `programming_examples/matmul_multi_core`, then triage — the negative control, so a test can tell "nothing wrong" from "something wrong" |
| `tt-watcher` | `noc-sanitize-trip` | same sanitize case, capturing `watcher.log` rather than the triage report |
| `tt-watcher` | `stack-usage` | `unit_tests_debug_tools --gtest_filter=MeshWatcherFixture.TestWatcherWaypoints` |
| `tt-dprint` | `tile-contents` | `unit_tests_debug_tools --gtest_filter=DevicePrintOutputFixture.PrintSimpleString` |
| `tt-checkpoint` | `cb-state` | `unit_tests_debug_tools --gtest_filter=DevicePrintCheckpointTest.DumpCB` |
| `tt-asserts` | `assert-fired` | a kernel `ASSERT(a != b)` under `TT_METAL_LIGHTWEIGHT_KERNEL_ASSERTS=1`, read back with `tt-triage.py --run=dump_lightweight_asserts` |
| `tt-profiler` | `device-zones` | `programming_examples/profiler/test_full_buffer` under `TT_METAL_DEVICE_PROFILER=1` |

The negative control is not optional. A skill that reports a fault for every
input passes every positive fixture.

## What is not captured yet

Nothing is. No fixture in this tree has been captured, because the machine the
skills were written on has no Tenstorrent device — no `tt-smi`, no
`/dev/tenstorrent*`, no tt-metal checkout. Every interpretation test therefore
skips today. Capturing these is the next real step, and it needs hardware.
