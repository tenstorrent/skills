# Claude bringup (experimental)

One Claude Code session brings a model up end to end: no multigoal runner, no stage reviews, no runner gates. The
session checks its own work and reports. Use the staged or lean pipeline when you need gated, comparable runs.

## Start

1. **Skills.** This variant is on the `mvasiljevic/lean-bringup-v2` branch of `tenstorrent/skills`, not on `main`
   yet: `git clone -b mvasiljevic/lean-bringup-v2 https://github.com/tenstorrent/skills.git`.
2. **Setup.** `setup.sh` prepares a run folder: tt-metal (with submodules, on a new branch `bringup/<model>`), the
   skills at the commit it comes from, the build, the venv and the weights. Each step is logged with its time to
   `<run dir>/setup.out`, which ends with `SETUP_DONE` or the step that failed; run it again after a fix (existing
   clones are kept).
3. **HF token.** Gated models and datasets (GPQA) need a token from an account that accepted their terms, at
   `$HF_HOME/token` or in the file `HF_TOKEN_PATH` names.
4. **Preflight.** `preflight.sh` checks the build, the weights (`--revision` for a pinned one; without it any
   snapshot in the cache counts), GPQA access, Docker, NFS mounts and that the mesh opens, and prints disk, RAM and
   firmware.
5. **Notes file.** A few lines for "This run" (see below).
6. **Launch** with a dedicated Claude config dir.
7. **Watch:** `tmux attach -t <session>` (detach with Ctrl-b d), `overseer.log` next to `inputs.json`.

```bash
SKILLS=$PWD/skills; P=$SKILLS/plugins/tt-model-bringup/prompts/model_bringup_claude
R=/data/runs/my-model                      # run folder; HF_HOME may live in it or elsewhere
export HF_HOME=$R/hf
$P/setup.sh --run-dir $R --hf-model org/model-name [--revision <sha>] [--tt-metal-ref <ref>]
P=$R/skills/plugins/tt-model-bringup/prompts/model_bringup_claude   # the skills commit setup.sh pinned
$P/preflight.sh --hf-model org/model-name --tt-metal $R/tt-metal [--revision <sha>]
$P/launch.sh --hf-model org/model-name --tt-metal $R/tt-metal --config-dir $R/claude-config \
  --notes $R/notes.md --budget 48 --resets yes
```

`launch.sh` writes `models/autoports/<model>/bringup/agentic_evals/inputs.json`, renders `brief.md` with a "This run"
section into `prompt.md` next to it, accepts the folder trust, starts Claude in tmux (`--permission-mode auto`),
tells the session when the budget is reached and starts the overseer. `CLAUDE_CONFIG_DIR`, `HF_HOME` and
`HF_TOKEN_PATH` are set in the session's command: a tmux server that already runs does not pass the caller's
environment to a new session. The tmux session, the budget watchdog and the overseer all use the caller's
`TMUX_TMPDIR`, so they reach the same tmux server. Options:

| Option | Default | |
|---|---|---|
| `--config-dir` | – | a dedicated Claude config dir for this run, created if missing (reused if present): the login copied from `~/.claude/.credentials.json` (or the current `CLAUDE_CONFIG_DIR`), a theme, onboarding done, the folder trust. Without it the run uses `CLAUDE_CONFIG_DIR` or `~/.claude` |
| `--run-dir` | parent of `--tt-metal` | added as a working directory (`--add-dir`, trusted ahead), so helper folders, environments and tools there are readable without permission dialogs; `HF_HOME` is added too when it is outside it |
| `--effort` | CLI default | Claude Code effort level (`low`, `medium`, `high`, `xhigh`, `max`) |
| `--scope` | `open` | or `contained` (changes stay in the model folder) |
| `--resets` | `no` | `yes` if bounded board resets are authorized on this machine |
| `--inputs` | – | extra `inputs.json` keys, e.g. `'{"targets": {"1": 25, "32": 40}, "evals": ["gpqa10", "swe10", "gpqa198", "swe100"]}'` (targets in decode ms per step) |
| `--notes` | – | a file appended to "This run" |
| `--session` | `bringup-<model>` | tmux session name |
| `--overseer-interval` | `1` | hours between overseer reports |
| `--no-overseer` | – | do not start the overseer |
| `--dry-run` | – | write `inputs.json`, `prompt.md` and the config dir, print the commands instead of starting them |

A dedicated config dir per run is the recommended way: the run gets no shared settings, skills, hooks, `CLAUDE.md`
or memory from your own Claude setup (memory is per project anyway), and its transcripts are easy to find. Claude
Code's default effort for `claude-opus-5-5` is medium; all runs so far used the default.

Reading outside the run folder is left to the agent: the config dir pre-accepts auto mode's one-time notice about reads outside the working directories, so a read elsewhere does not stop the run at a dialog, and the prompt's "This run" section asks it to read other folders only when that helps and never change them. The run folder (`--run-dir`, default the folder above the checkout, never your home) is a working directory.

Several runs on one host: give each its own `--config-dir` and `--session` (and its own `TMUX_TMPDIR` if you want
separate tmux servers; then attach with the same `TMUX_TMPDIR`).

## The notes file

It holds the limits you set and the facts the agent cannot find itself; it is appended to the brief as is. Limits:
the work folder, what may be installed, host rules (e.g. a CI runner that must not be touched), resets. Facts: host
history (crashes, a board that needed a power cycle), local paths, pinned weights. Not model facts the agent can read
from the checkpoint and its config. Keep it short. For example:

```
- Install what you need inside /data/runs/my-model (packages, tools, extra environments); keep tt-metal's python_env able to run device code.
- This host is also a CI runner: do not touch the runner service or its containers.
- Weights are pinned to revision <sha> in $HF_HOME; do not download another.
- The host lost the PCIe link to one board last week; it came back after a cold power cycle.
```

## Overseer

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

If a host is lost or a session ends, see `RESUME.md`.
