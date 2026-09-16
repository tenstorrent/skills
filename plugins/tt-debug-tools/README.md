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
| Read or poke one address, register or RISC | `tt-exalens` |

Several of these are mutually exclusive — watcher, kernel prints and the device
profiler share on-chip SRAM, and enabling a second one corrupts the first one's
data rather than failing. Each skill names its conflicts.

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
