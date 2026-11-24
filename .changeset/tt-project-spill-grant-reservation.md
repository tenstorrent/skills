---
"tt-project": patch
---

Checks spilled to another host no longer over-admit it when runs start together. A run now checks the host's load again, sizes its test workers and records its grant while holding that host's reservation lock, which every project on the machine shares. Runs starting seconds apart therefore each see the others' grants instead of the same idle cores. A grant counts as the host's load until its run ends or for `checks.spill_hold_s` (default 90 s), and a dead run's grant counts for nothing. No check starts on a host whose 1-minute load plus those grants is at or above `checks.spill_load_near` x its cores (default 0.75, previously a fixed 0.8). Both are account settings. Runs pinned to a check host use the same gate and hold.
