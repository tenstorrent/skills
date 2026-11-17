---
"tt-project": patch
---

`tt-project`: model-bench: Terminal-Bench chat route with server verdicts, reruns on a healthy server and a table.md per run; parity section (same task set, limit, rerun rule; how each arm was called) in the Terminal-Bench and daily tables; every scheduled bench cycle raises one event on its third failure in a row (t8716).
- model-bench terminalbench: look for the compose plugin in the bench's own folder (terminalbench/compose), where its docker config link lives, not in the harbor venv folder (t8716)
