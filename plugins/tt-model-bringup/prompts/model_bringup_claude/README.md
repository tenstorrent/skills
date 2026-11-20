# Claude bringup (experimental)

One Claude Code session brings a model up end to end with `scope` `open`: no multigoal runner, no stage
reviews, no runner gates. The session checks its own work and reports. Use the staged or lean pipeline when you
need gated, comparable runs.

## Start

In the tt-metal checkout, write `models/autoports/<model>/bringup/agentic_evals/inputs.json`:

```json
{"scope": "open", "budget_hours": 48, "batch_sizes": [1, 8, 16, 32]}
```

Put in the run's brief (or tell the session) whether bounded board resets and health checks are authorized on
this machine. Then, in tmux so the run outlives the terminal:

```bash
SKILLS=/path/to/tenstorrent/skills
claude --model claude-opus-5-5 \
  --plugin-dir "$SKILLS/plugins/tt-model-bringup" --plugin-dir "$SKILLS/plugins/tt-autodebug" \
  "$(sed "s|HF_MODEL|org/model-name|g" "$SKILLS/plugins/tt-model-bringup/prompts/model_bringup_claude/brief.md")"
```

Pick a permission mode that lets device jobs and builds run while nobody is watching, or approve as it goes.
