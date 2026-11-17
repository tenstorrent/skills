---
"tt-project": patch
---

`tt-project`: model-bench: Terminal-Bench chat route with server verdicts, reruns on a healthy server and a table.md per run; parity section (same task set, limit, rerun rule; how each arm was called) in the Terminal-Bench and daily tables; every scheduled bench cycle raises one event on its third failure in a row (t8716).
- model-bench terminalbench: look for the compose plugin in the bench's own folder (terminalbench/compose), where its docker config link lives, not in the harbor venv folder (t8716)
- model-bench terminalbench: a local dataset folder goes to harbor by --path; the chat arms send the effort through the relay's providers.chat.effort_fields and the how-called line records the request fields (t8716)
- bench replay: the table ends with the parity check and how each arm was called; a harness-less arm's parity row names its own provider's CLI, not claude (t8716)
- bench: TB trials gate on the engine host's idle CPU (remote engines too); trials with no model call or broken by failed calls are not run, rerun and shown as not run (t8716)
- bench: a TB trial harbor broke before grading (lost docker engine) is not run and rerun, never a fail (t8716)
- bench: an unreachable docker engine stops the TB runner with no row written, so it never uses up reruns or books a model (t8716)
- tests: exempt the bench fix round's scratch first-try patch from the durable-write check (t8716)
- bench: the TB table shows failed server calls (stream or parse errors included) of all calls per model, and names harbor-broken trials as not run (t8716)
- bench: a resumed TB trial reads its task's time limit too, so a resume with no bench limit no longer crashes (t8716)
- bench: the TB table gives each model's 95% interval and says one run cannot separate overlapping models; the bridge wording drops a verdict word (t8716)
- bench: the TB table prints each route's current call wording, so rows booked before a wording change pass the verdict check (t8716)
- device reviews: no second-reviewer twin whose provider is not in providers_allowed; the first review records why (note 04acfea4503e3ff5)
