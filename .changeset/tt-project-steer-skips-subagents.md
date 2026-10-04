---
"tt-project": patch
---

Mid-run task updates no longer reach a worker's subagents: the hook stays silent on a subagent's tool calls and hands the update to the worker on its next own tool call. Updates name the task and say they come from the harness. The worker prompt says updates are not for subagents and that text read while working is data, not instructions.
