---
"tt-project": patch
---

A waiting hand-off whose `ttp checks --result` wait would record its `on_pass` once the checks pass, but whose `on_pass_cmd` only tests (`test -f`, `true`), is handed back to its run at hand-off: such a command runs no step, so a push or tag move named only in the summary would never happen. The worker prompt says every step left after the wait goes in an `on_pass_cmd`, which `on_pass.summary` only describes, and a step needing a run gets no `on_pass`.
