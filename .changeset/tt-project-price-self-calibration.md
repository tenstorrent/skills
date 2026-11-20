---
"tt-project": patch
---

`tt-project`: the estimate of other Claude Code sessions corrects its own price-table drift. Once 3 finished runs mostly on one model agree within 5% on reported cost over list price, later calls on that model are scaled by that factor (bounded to 0.5-2.0, recorded in `~/.tt-project/price-calibration.json`, dropped when the model's list price changes), so the global daily cap counts the measured cost. The `claude_price_table` alert no longer fires daily at 1% drift or asks the user to edit the table: one low alert per model and price-table row says what was done (a new release with the same table does not repeat it), only for a drift past 20%, a factor out of bounds (not applied) or a model with no price.
