---
"tt-project": patch
---

`tt-project`: the charter conflict check no longer pairs a scoped exception ('Exception to "<clause>" ...') with a ban as its permitting side. Its scope was checked when it was added, so an exception that shares wording with a second ban is not reported as conflicting with it. A plain permitting item that contradicts a ban is still paired.

`tt-project`: a task pinned to a model pins its subagents too. On Claude Code (and the chat provider) the worker run gets `CLAUDE_CODE_SUBAGENT_MODEL` set to the pinned model, unless the daemon's environment already sets it explicitly. Unpinned tasks are unchanged. The providers doc lists which providers have such a switch.
