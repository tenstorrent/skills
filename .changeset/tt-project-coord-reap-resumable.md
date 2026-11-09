---
"tt-project": patch
---

A coordinator answer is never lost. Reaping a coordinator run is never stopped by the tick step budget. A daemon cut off mid-apply resumes the turn from its `apply.json` journal: an action cut off twice is skipped and refused back to the coordinator, the other actions apply once, and the run is never quarantined. A coordinator run that exited 0 never counts as runaway waste when its reap fails; it raises one `coordinator_reap_failed` event instead. `watchdog.step_budget_s` is a known config key. A tick step stopped at its budget on the same item three times in a row raises one `tick_step_stuck` event.
