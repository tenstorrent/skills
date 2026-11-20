---
"tt-project": patch
---

`tt-project`: a wait held by a missing user marker (`test -e state/user-words/user-x && <check>`) now times its live part from when the marker appeared (its mtime), not from the start of the wait. Once the user writes the marker, neither a probe stall nor `wait_stale` is raised until the stall threshold or `waiting.max_hold_s` has passed since then, so the coordinator is not woken the moment the user acts.
