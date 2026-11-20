---
"tt-project": patch
---

`tt-project`: GLM guard follow-ups.

- A GLM device run gets one scratch folder per other host its plan reaches, `/tmp/ttp-<task id>`. The harness sets the path from the run's own task id and passes it to the planner, so a plan naming another task's folder, or none, cannot send GLM to the wrong place. GLM writes, copies and removes inside that folder on that host count as in plan; any other write there, a link, `sudo` or an onward ssh still hands the run back. `ttp advise --plan` prints the folder under the plan, the GLM device prompt points to it, and the post-run scan no longer lists writes inside it.
- The tool hook now applies the GLM guard (with its scratch redirect to the run's own temporary folder) on every route that sends a run to the chat model, not only the code arm and coordinator: a GLM work run writing to `/tmp` is redirected like a code-arm run.
- A run whose spec has a `conclusion:` line (a result the user reads) stays on the core model when GLM work routing is on, as it already did for the code arm and tier routing.
- New regression cases for `sed -i` substitutions and python heredocs the guard once read as deletes.
