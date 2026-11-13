---
"tt-project": patch
---

`tt-project`:

- Re-booking a resumed Codex run reads its session's rollout (and its sub-agents') only up to the run's end, so a later wake on the same session never moves its usage onto an earlier run; runs already re-booked with a later wake's usage are corrected once, with a receipt naming each row's old and new charge
