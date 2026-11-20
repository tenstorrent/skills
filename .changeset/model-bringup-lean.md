---
"tt-model-bringup": minor
---

Add a lean pipeline next to the staged one: goal `1-model` (stages 1-8 as one outcome-defined goal led by a
coordinator with subagents, `lean-model` skill, `check_model_goal.py`), goal `2-serve` (stages 9-11, gated by
their existing checks), then the unchanged agentic-evals goal 12. Outputs and tests are fixed; the order of work
is the agent's.
