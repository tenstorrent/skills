---
"tt-project": patch
---

`tt-project`: model-bench suite grades a held-out test in a case file through its aggregator (as bench prep validates it) instead of failing every such cell on collection; a cell an endpoint fault ended (an HTTP 429) is retried after a growing pause and never scored, and a mostly faulted model is inconclusive; the arms run side by side, one cell at a time each, with appends and verdict bookings serialized; each cell's row goes to `<date>-cells/results.jsonl` as it ends; a head-to-head rerun retries endpoint-faulted cells.
