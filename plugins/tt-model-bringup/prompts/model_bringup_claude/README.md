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
section into `prompt.md` next to it, accepts the folder trust, starts Claude in tmux (`--permission-mode auto`) and
tells the session when the budget is reached. Options:

| Option | Default | |
|---|---|---|
| `--scope` | `open` | or `contained` (changes stay in the model folder) |
| `--resets` | `no` | `yes` if bounded board resets are authorized on this machine |
| `--inputs` | – | extra `inputs.json` keys, e.g. `'{"targets": {"1": 25, "32": 40}, "evals": ["gpqa10", "swe10", "gpqa198", "swe100"]}'` (targets in decode ms per step) |
| `--notes` | – | a file appended to "This run" (host rules, e.g. "CI runner: don't touch the runner or its containers") |
| `--session` | `bringup-<model>` | tmux session name |

`preflight.sh` checks the build, the weights, HF access to the gated GPQA dataset, Docker, NFS mounts and that the
mesh opens, and prints disk, RAM and firmware. If a host is lost or a session ends, see `RESUME.md`.
