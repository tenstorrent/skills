---
"tt-project": patch
---

`tt-project`: a running Codex run's plan windows (used percent and reset time) are read from the `rate_limits` of its session rollout's token_count events, in the same incremental pass that prices it mid-turn. The daemon records the newest reading once, at the time it was taken, while it is at most 3 minutes old, so the budget gate sees fresh Codex windows with each model call instead of only every 10 minutes from the app-server meter. An absent, stale or malformed reading, or one for another limit, records nothing and the meter's reading stands. No new model or app-server calls.
