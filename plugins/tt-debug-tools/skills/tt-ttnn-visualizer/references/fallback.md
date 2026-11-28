# Without the MCP server

The tools come from the `ttnn-visualizer-mcp` stdio server, shipped in the
`ttnn-visualizer` Python package. This plugin does not register it.

## Getting it

The user installs and registers it; do not do either yourself, and do not edit
MCP config. Point them at the current install instructions in the
ttnn-visualizer docs — v0.110.0 or later; upgrade an older install — then
registration, e.g.:

```bash
claude mcp add ttnn-visualizer -- ttnn-visualizer-mcp
codex mcp add ttnn-visualizer -- ttnn-visualizer-mcp
```

A newly registered server appears only in a new session. The server records
tool names and outcomes to a local usage log; the visualizer docs describe the
opt-out.

## Until then

**Performance questions** — `tt-profiler`'s `tt-perf-report` recipe answers
"which op dominates" and gives per-op bound and advice from the same CSV.
Signposts work the same way (`--start-signpost`, `--end-signpost`).

**Memory, crash and provenance questions** — give the user this command to
open the report in the app; do not start it yourself:

```bash
ttnn-visualizer --host 127.0.0.1 --memory-report-path '<report folder>' --performance-path '<perf folder>'
```

Pass only the folders the user has: drop `--performance-path` for a memory
report alone, or `--memory-report-path` for a performance report alone.
Single-quote the paths, so the shell expands nothing in them. tt-metal makes
safe folder names, but a shared or renamed report can hold anything: if a path
contains `'`, `$`, a backtick, `;` or a newline, do not build the command —
tell the user the folder name is unsafe and ask them to rename it. Keep
`--host 127.0.0.1` — inside a container the app otherwise listens on every
interface, and a local visualizer opens any folder it is pointed at. Never
`--server` for this. Then say which view answers the question (operations
list, L1 memory, buffers, performance). There is no CLI equivalent of
`errors` or `l1_peak`.

**In-process alternatives** — inside the run itself, `tt-ttnn-flags` graph
capture gives `extract_peak_L1_memory_usage` and the call trace, without any
report on disk.

## Do not

- Hand-write SQL against `db.sqlite`. Its schema is internal to tt-metal and
  the visualizer, changes between releases, and the per-bank and id-space rules
  the tools enforce are easy to get wrong by hand.
- Parse the perf CSV for a question `tt-perf-report` already answers.
