---
"tt-project": patch
---

`tt-project`: a waiting hand-off may list `then`, up to 4 later waits (`retry_when`, optional `on_pass_cmd` and `retry_after_s`). The daemon moves from one stage to the next model-free, keeps the stage in the task's stored hand-off across restarts, starts each stage's own wait clock (so the stale-wait cap counts from that stage), records `on_pass` after the last, and wakes the worker at its own tier only on a failed stage, a broken probe or an end without a done hand-off. A malformed list is refused at hand-off.
