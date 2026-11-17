---
"tt-project": patch
---

`tt-project`: model-bench: Terminal-Bench chat route with server verdicts, reruns on a healthy server and a table.md per run; parity section (same task set, limit, rerun rule; how each arm was called) in the Terminal-Bench and daily tables; every scheduled bench cycle raises one event on its third failure in a row (t8716).
- model-bench terminalbench: look for the compose plugin in the bench's own folder (terminalbench/compose), where its docker config link lives, not in the harbor venv folder (t8716)
- model-bench terminalbench: a local dataset folder goes to harbor by --path; the chat arms send the effort through the relay's providers.chat.effort_fields and the how-called line records the request fields (t8716)
- bench replay: the table ends with the parity check and how each arm was called; a harness-less arm's parity row names its own provider's CLI, not claude (t8716)
- bench: TB trials gate on the engine host's idle CPU (remote engines too); trials with no model call or broken by failed calls are not run, rerun and shown as not run (t8716)
