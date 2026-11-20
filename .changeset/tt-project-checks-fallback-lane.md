---
"tt-project": patch
---

`tt-project`: `checks.fallback_lane` (project setting, default off) names where the commands behind `checks.wrap_cmd` run while that wrapper's lock is paused: another wrapper command (a CPU-only pool, say) or `unwrapped` (no wrapper, `TTP_NO_DEVICE=1`). The pass line and checks.json say which lane ran them and which lock is paused; a fallback that is paused too or cannot serve waits on the pause and never fails. Off, a paused wrapper waits as before.
