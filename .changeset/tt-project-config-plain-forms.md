---
"tt-project": patch
---

`tt-project`: the config type check takes the plain forms the runtime reads: `budget.compact_window_tokens` and `budget.split_reread_tokens` take one non-negative number for every tier (0: off), and `budget.resume_lost` and `waiting.wake_gate` take `false` (off), as well as an object. Negative numbers, strings and other types are still refused, naming the forms allowed. The coordinator's config_set and the web app's settings now run the same check as `ttp config`.
