---
"tt-project": patch
---

A new coordinator session's seed cap bounds each past new-session wake's seed by that wake's seeded
prompt size, now recorded as `turn.session.seed_chars` (system, context and prompt with the memory,
order ledger, handover note and chat), not by its size before the seed. A seed that grew with chat
and the handover note is no longer clipped, so the next first wake's cap covers it. Older wakes
without the field keep the bound they had. The guide states how the bound's factor was calibrated.
