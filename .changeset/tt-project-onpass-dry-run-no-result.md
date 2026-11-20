---
"tt-project": patch
---

`tt-project`: the hand-off dry run of an `on_pass_cmd` now hands the run back (as a crash does) when the command exits 0 but would not record done once its probe passes: no hand-off written to `$TTP_RESULT` (unless it exits early on `TTP_ON_PASS_DRY_RUN` on purpose or a `ttp checks --result` wait's `on_pass` is the fallback), one that is no JSON object, or one carrying a wait. A command that sets `TTP_RESULT` itself is not run and is handed back the same way, and an empty `on_pass_cmd` is dropped from the hand-off.
