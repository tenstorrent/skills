---
"tt-project": patch
---

`tt-project`:

- coordinator session wakes: the per-wake dollar cap is sized to the session's measured wakes (twice the dearest recent ok resumed wake, at least coordinator.turn_budget_usd, within the dollar caps) instead of a fixed $1; a new session's seed raise is bounded only by the project and account dollar caps, never the hourly coordinator limit
- a new session's first wake that did work and then hit its dollar cap or timeout keeps its session: the next wake resumes it instead of seeding a new one; tests
