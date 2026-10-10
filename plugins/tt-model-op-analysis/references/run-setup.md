# Run setup (both skills)

## Interactive gate

Do not treat open files, IDE selections, shell history, environment variables, or a previous run
as user confirmation. Ask exactly one setup question per turn and stop for the answer. Never ask
the user to confirm the whole configuration in one message.

Use this order, skipping only values the user explicitly stated in the current conversation:

1. Ask which model to analyse. Do no model-specific discovery before the answer.
2. Discover the model directory and candidate entry tests. Propose one node ID and ask whether
   to use it. Stop for the answer.
3. When multiple implementations or ports exist, ask which one to analyse. Stop for the answer.
4. Ask which source ref to pin. A suggested ref is allowed, but it is not confirmation. Stop.
5. Ask for the output root, explain it cannot be inside the tt-metal checkout, and stop.

After this shared setup, the selected workflow asks its own remaining questions one at a time
(for example static target profiles or measured warm/cold handling). Do not inspect model source,
create output, or delegate tracing until every required answer is confirmed.

## 1. Model and test

Ask which model. In the tt-metal checkout, find:

- the model directory (`git ls-tree -r --name-only <sha> models/ | grep -i <model>`),
- the entry function the tests call,
- the tests that drive it.

Propose one test: the end-to-end PCC test, or the test the device-perf CI runs (look for the
`command = f"pytest ...` string in `test_perf_device*.py`). Show the node id and ask the user to
confirm. If a Blackhole and a Quasar implementation both exist (for example
`models/demos/vision/classification/resnet50/ttnn_resnet` and `.../resnet50/quasar`), ask which
code is analysed; the Quasar port goes in the `quasar:port` column, never mixed into the
Blackhole rows.

## 2. Pin the source

Record the full SHA of the commit analysed (`git rev-parse <ref>`; prefer a freshly fetched
`origin/main` unless the user names a ref). Read every file as `git show <sha>:<path>`; never read
the working tree. For Quasar, record the ttsim version if the user knows it.

zsh trap: `$VAR:path` applies the `:t`/`:h` modifiers. Write `<sha>:path` with the literal SHA or
use `"${VAR}:path"`.

## 3. Output location and previous runs

Root: current working directory unless the user names one; never inside the tt-metal checkout.
Run directory: `<root>/op-analysis/<model>/<static|measured>/<YYYY-MM-DD>_<sha7>/`.

`run.json` fields:

```json
{"kind": "static", "model": "resnet50", "test": "<node id>", "targets": ["p150", "quasar"],
 "tt_metal_commit": "<40-char sha>", "date": "YYYY-MM-DD", "drive_url": null,
 "port_e2e": null}
```

`port_e2e`, when the user reports a passing Quasar-port e2e run: `{"by": "...", "date": "...",
"config": "ttsim <version>, <grid>, batch <n>, <dtype>", "result": "<pcc/top-1 line>"}`.

If `op-analysis/<model>/<kind>/` already has a run, ask once:

- **overwrite**: delete that run directory and write the new one; if it has a `drive_url`,
  update the same spreadsheet.
- **new**: new directory; the old one is untouched.
- **merge**: new directory, then for each table below present in both runs
  `python3 <plugin-root>/scripts/compare_runs.py --old <old>/<t>.csv --new <new>/<t>.csv --out <new>/changes_<t>.csv --key <key>`,
  and a "Changes since <old run>" section in `summary.md` listing the commit range and the
  counts per change type. On Drive, add a `Changes` tab to the existing spreadsheet.

  | Table | `--key` |
  |---|---|
  | `op_table.csv` | default (`stage,ttnn_api,device_op,program_factory`) |
  | `call_trace.csv` | `profile,stage` |
  | `quasar_blockers.csv` | `blocker` |
  | `host_ops.csv` | `host_work` |
  | `host_fallback.csv` | `scope` |
  | `footprint.csv` | `op_code` |

  `measured_ops.csv` and `diff.csv` are not compared: per-op timings change every run, and the
  diff is regenerated. `compare_runs.py` exits 2 if a key column is missing.
