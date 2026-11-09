---
"tt-project": patch
---

`tt-project`: every finished Claude run (workers, reviewers, coordinator wakes; fresh, resumed or forked) is now booked at what its own API calls cost: each call the run's output names counted once, at the final token counts Claude Code's session log recorded, at list price, with 1-hour cache writes priced at the 1-hour rate, subagent calls included and other vendors' models (GLM) left out. Claude Code's reported total, which in a resumed or forked session is the whole session's running total, is no longer booked; it is kept in the run's note, and still sets the coordinator's per-wake cap. A run that made no call books $0. On its next start each project re-books, once, its Claude runs of the last 7 days at their per-call cost, up or down, with matching task spend and ledger rows, so the dollar caps and the daily total count each call once; the daemon log says how many runs moved and the dollars before and after.
