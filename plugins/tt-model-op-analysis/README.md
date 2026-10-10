# tt-model-op-analysis

Kernel-op evidence for one tt-metal model and its entry test.

```bash
/plugin install tt-model-op-analysis@tenstorrent-skills     # Claude Code
codex plugin add tt-model-op-analysis@tenstorrent-skills    # Codex
```

| Skill | Output |
|---|---|
| `static-op-analysis` | `op_table.csv`, `call_trace.csv`, `quasar_blockers.csv`, `host_ops.csv`, `summary.md` for P100, P150 and/or Quasar |
| `measured-op-analysis` | `measured_ops.csv`, `host_fallback.csv`, `footprint.csv`, `diff.csv`, `summary.md` from a Tracy run |

Both start from the same model and test, pin the tt-metal commit, and keep each run in
`op-analysis/<model>/<static|measured>/<date>_<sha7>/` with a `run.json`. A rerun can overwrite,
start a new run, or merge with `changes_<table>.csv` files against the previous one. Upload to Google Drive
happens only when the host has a Drive/Sheets connector and the user agrees.

A generic plugin invocation first asks whether to run `static-op-analysis` or
`measured-op-analysis`. Each workflow then confirms its model, test, implementation, source ref,
and output root instead of inferring them from IDE state.

Counts, totals, schema checks and diffs come from the scripts in `scripts/`, not from the agent.
The measured skill needs a Tenstorrent device and a built tt-metal checkout; it routes the run
through `tt-device-mcp` when available.
