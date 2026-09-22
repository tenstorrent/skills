# Flags

## Environment

| Env var | Effect |
|---|---|
| `TT_TRIAGE_ENABLE_AGGREGATED_CALLSTACKS=1` | Swap raw `dump_callstacks` for `dump_aggregated_callstacks`. Off by default; the aggregated view groups cores by stuck point and prints one row per group with a count, which is what makes a many-core or multi-device hang readable at all. |

## Command-line

| Flag | Effect |
|---|---|
| `--llm-output` | CSV instead of Rich tables. Cheaper to read and greppable. Implies no colours and no progress bar. Use it always. |
| `--llm-output-path=<p>` | Write the CSV report to a file. |
| `--triage-summary-path=<p>` | Write the one-line-per-script summary. |
| `--run=<script>` | Run one script instead of all. Dependencies and patches still apply, so this is the cheap way to re-check one thing after a full pass. Repeatable. |
| `--dev=in_use \| all` | Which devices to analyse. `in_use` is the default; pass a device id to narrow, repeat the flag for several. |
| `-v` | More callstack columns: firmware and kernel path, host-assigned ID, kernel offset, previous kernel. |
| `-vv` | Adds read pointer, base and offset, and the kernel XIP path. |
| `--all-cores` | Include cores whose Go Message is `DONE`. Filtered out by default; reach for it only when a `DONE` core is suspect. |
| `--initialize-with-noc1` | Initialise the debugger context over NOC1. Use when NOC0 is not functioning. |
| `--remote-exalens` | Talk to a `tt-exalens --server` running in another shell instead of initialising UMD directly. |
| `--skip-version-check` | Bypass the tt-exalens version pin. An escape hatch — you own what follows. |

## Multi-rank

`tt-run-triage.py` wraps `tt-run`, runs one full pass per rank under `mpirun`,
and merges all ranks into one stream with a section per script. Everything after
`--` is forwarded verbatim to every rank.

Forward `--triage-summary-path` and each rank appends a `_rank_<N>` suffix, so
paths do not collide.

`--rank-binding=<bindings.yaml>` must match how the workload was launched. If you
launched it, reuse that exact binding. If the binding is not recoverable, ask
rather than guessing — a wrong binding produces a report about the wrong ranks.

## The callstack's op id

The `Host Assigned ID` column, shown at `-v`, is the **op id** of the op running
on that core when non-zero — the same id the op-level scripts print. It is what
ties a stuck core back to its op.
