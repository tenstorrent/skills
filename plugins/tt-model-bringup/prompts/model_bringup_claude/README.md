# Claude bringup (experimental)

One Claude Code session brings a model up end to end: no multigoal runner, no stage reviews, no runner gates. The
session checks its own work and reports. Use the staged or lean pipeline when you need gated, comparable runs.

## Start

In a built tt-metal checkout (`./build_metal.sh`, `./create_venv.sh`) with the weights in the HF cache:

```bash
SKILLS=/path/to/tenstorrent/skills; P=$SKILLS/plugins/tt-model-bringup/prompts/model_bringup_claude
$P/preflight.sh --hf-model org/model-name --tt-metal ~/tt-metal
$P/launch.sh --hf-model org/model-name --tt-metal ~/tt-metal --budget 48 --resets yes
```

`launch.sh` writes `models/autoports/<model>/bringup/agentic_evals/inputs.json`, renders `brief.md` with a "This run"
section into `prompt.md` next to it, accepts the folder trust, starts Claude in tmux (`--permission-mode auto`),
tells the session when the budget is reached and starts the overseer. Options:

| Option | Default | |
|---|---|---|
| `--scope` | `open` | or `contained` (changes stay in the model folder) |
| `--resets` | `no` | `yes` if bounded board resets are authorized on this machine |
| `--inputs` | – | extra `inputs.json` keys, e.g. `'{"targets": {"1": 25, "32": 40}, "evals": ["gpqa10", "swe10", "gpqa198", "swe100"]}'` (targets in decode ms per step) |
| `--notes` | – | a file appended to "This run" (host rules, e.g. "CI runner: don't touch the runner or its containers") |
| `--session` | `bringup-<model>` | tmux session name |
| `--overseer-interval` | `1` | hours between overseer reports |
| `--no-overseer` | – | do not start the overseer |

The overseer (`overseer.sh`) reports facts; the session diagnoses and decides. Each tick it starts one fresh
`claude -p` agent (default `claude-opus-5-5`, `--model` to change; no saved session, 20 min limit) with a short
prompt in the script. The agent gets a time window, from the previous report (its time and text, taken from
`overseer.log`) or the run start to now, and reads only that window: transcript records newer than the window start
(`jq` on `.timestamp`), files newer than it (`find -newermt`, `stat`, `tail`). Its sources are the session's
transcripts (`$CLAUDE_CONFIG_DIR` or `~/.claude`, `projects/<checkout path, / and . as ->`: the main `*.jsonl`,
`*/subagents/*.jsonl`, `tool-results/`), the background shell outputs under `/tmp/claude-<uid>`, `inputs.json` and
the host (`ps`, `fuser` on `/dev/tenstorrent/*`, `free`, `df`). It first runs a fixed `jq` command (given in the
prompt) that lists every subagent and background job of the main session and its subagents with its owner, start
and whether it has finished (task notifications, `TaskStop`), so none is skipped. Then, one line each for the main
session and every unfinished item, it says what progressed in the window (new results, files written, logs that
grew; tool calls or a poll loop alone are not progress) or "NO PROGRESS since <time>", whether the previous report
also showed none (so a stall over several hours is visible), and the sizes and times the next check can compare
against. One line covers resources: devices (who holds them, busy or idle), CPU and memory, disk and budget. The
report is typed into the session prefixed "Automated overseer report (hourly, not from the operator):" and appended
to `overseer.log` next to `inputs.json`, stamped with the window end. When the pane shows a dialog or prompt, nothing
is typed (the keys could answer it); the next tick tries again. A failed tick is logged and sends nothing.

Read-only is enforced by the tool settings, not only the prompt: `--tools Read,Grep,Glob,Bash`,
`--allowedTools` with read-only commands only (`ps`, `tail`, `grep`, `jq`, `ls`, `df`, `fuser`, `cat`, `find`, ...),
`--disallowedTools` for edits, `tmux`, `kill`, `rm`, `git`, `tt-smi` and `find -delete/-exec`, and
`--permission-mode dontAsk` so anything else is denied, not asked. `--restricted` ignores user and project
settings (their allow rules and hooks), and limits file access to the transcripts, the checkout, `/dev/tenstorrent`,
the run folder and `/tmp/claude-<uid>`; `--strict-mcp-config` loads no MCP servers.

To try it without sending, on the live run or on a past one (`--at` copies the transcripts cut at that time and
tells the agent to ignore live host state; `--since` sets the window start, and a dry run reads the previous report
from `--log` but does not write it):
`overseer.sh --dry-run --once --tt <tt-metal> [--inputs <inputs.json>] [--project-dir <claude config>/projects/<dir> --at <UTC time>] [--since <UTC time>] [--log <file>]`.

`preflight.sh` checks the build, the weights, HF access to the gated GPQA dataset, Docker, NFS mounts and that the
mesh opens, and prints disk, RAM and firmware. If a host is lost or a session ends, see `RESUME.md`.
