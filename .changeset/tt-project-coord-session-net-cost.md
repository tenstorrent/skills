---
"tt-project": patch
---

`tt-project`: a resumed coordinator session wake is booked at its own cost, not the session's running total. Claude Code reports a resumed session's whole cost as `total_cost_usd`; the daemon now nets it over the session's earlier wakes with the same token-price agreement check as worker resumes, so project spend, caps, stats and the coordinator session spend guard read each wake once. A resumed wake's `--max-budget-usd` is what its session already booked plus the per-wake cap, since the CLI holds its running total to that flag. On its first start the daemon re-nets the resumed wakes already booked, once, and logs each change. The price-table calibration compares a resumed wake's whole session log with the session's reported total.
