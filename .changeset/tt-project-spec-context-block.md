---
"tt-project": patch
---

Coordinator and planner prompts: the coordinator thinks a task through when it gets or starts it,
and writes the plan or hands the planner every key point. Every spec, plan brief and mid-run
correction carries a short `Context:` block of what the worker cannot see: the user's intent and
verbatim words with message ids, decisions, the binding Restrictions, earlier findings and failed
attempts (result.json paths), relevant memory, and related tasks and files. Both coordinator modes
carry the rule; a plan gives each task it proposes the same block.
