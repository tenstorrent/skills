# tt-debug-tools

Drive the Tenstorrent debug tools and read their output. One skill per debugging
question — the router picks by the question you are asking, not by the tool you
have heard of.

```bash
/plugin install tt-debug-tools@tenstorrent-skills     # Claude Code
codex plugin add tt-debug-tools@tenstorrent-skills    # Codex
```

Skills are selected automatically as the task requires; you can also name one.

Needs a Tenstorrent device and a built tt-metal checkout to be useful. Several
of these skills change device state.

## Which skill

| Question | Skill |
|---|---|
| The device is hung or stuck — what was it doing? | `tt-triage` |
| Is a kernel writing to the wrong place, or overflowing a CB? | `tt-watcher` |
| What values does my kernel actually see? | `tt-dprint` |
| Is my assumption true *inside* the kernel? | `tt-asserts` |
| Which data-movement call is missing a barrier? | `tt-noc-dump` |
| What does the host runtime think is running? | `tt-inspector` |
| Where is device time going? | `tt-profiler` |
| Which op hangs, and which op loses precision? | `tt-ttnn-flags` |
| Why did this op crash or run out of L1, and what does my report say? | `tt-ttnn-visualizer` |
| Read or poke one address, register or RISC | `tt-exalens` |

Several of these are mutually exclusive — watcher, kernel prints and the device
profiler share on-chip SRAM, and enabling a second one corrupts the first one's
data rather than failing. Each skill names its conflicts.

## Optional: TT-NN Visualizer MCP server

`tt-ttnn-visualizer` reads reports through the read-only MCP server that ships
with the `ttnn-visualizer` Python package. This plugin does not register it.
It needs ttnn-visualizer v0.110.0 or later. Install the current release per
the ttnn-visualizer docs, then:

```bash
claude mcp add ttnn-visualizer -- ttnn-visualizer-mcp     # Claude Code
codex mcp add ttnn-visualizer -- ttnn-visualizer-mcp      # Codex
```

Start a new session afterwards. Without it, the skill falls back to
`tt-perf-report` and to opening the report in the visualizer app. The server
records tool names and outcomes to a local usage log; the visualizer docs
describe the opt-out.

## Evals

Each skill has an eval next to it under `evals/<skill>/eval.py`. They drive a
real agent with the plugin loaded and grade the answer. Non-deterministic and
priced per run, so **never in CI**. Full protocol and how to add one are in
[`evals/README.md`](evals/README.md).

```bash
python plugins/tt-debug-tools/evals/run.py                    # all skills
python plugins/tt-debug-tools/evals/run.py --skill tt-triage  # one skill
```

## Layout

```
skills/<name>/           SKILL.md and any references it loads on demand
evals/<skill>/           eval.py, and any fixtures/, provoke/ or README.md it needs
evals/run.py             the entrypoint
evals/harness.py         the agent handle and its device-launcher method
evals/capture_fixtures.sh
```
