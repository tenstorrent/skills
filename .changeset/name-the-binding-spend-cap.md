---
"tt-project": patch
---

budget: when a project's own `budget.global_daily_usd` differs from the account-level cap, the digest's budget line, `ttp status` and the web spend view call it the project cap (below or above the account cap), and a hold it causes reads "project daily cap $X reached" instead of naming the account cap.
