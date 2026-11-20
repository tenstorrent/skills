---
"tt-project": patch
---

`tt-project`: a code task whose spec has a line starting `premise: <read-only check>` now runs that check first as a light question task. The code task starts only when the check's hand-off says `"premise": "holds"`, and the check's summary is added to its spec. When it says `"fails"`, the code task is closed as done, not needed, with the check's summary, and the coordinator gets the usual done event. When the check gives no verdict, fails or is cancelled, the code task runs anyway and checks the premise itself. The conclusion floor does not lift the check, and its own done event starts no coordinator turn. `ttp audit` adds a line `code runs with no code change: N, $X`, which also counts the code tasks a premise check closed. The coordinator prompt says when to use `premise:`.
