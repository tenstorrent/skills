---
"tt-project": patch
---

`tt-project`: a waiting hand-off may name `probe_match` (`pass`, `fail`, `wait`: plain substrings of the last 4,000 chars of its `retry_when` probe's output as printed), so a confirm or watch wait ends with no model run. A probe that exits 0 with only `wait` matching counts as "not yet" (a timer wake re-defers with the wake gate's back-off, even when a progress count changed); only `pass` records the hand-off's `on_pass` model-free, with `{probe_out}` in its summary or message filled from the output; only `fail` wakes the task at its own tier with the output's tail; no match or several still wake the model. Each settled check is written to the task's notes, and a malformed `probe_match` is refused with a `handoff_probe_refused` event.
