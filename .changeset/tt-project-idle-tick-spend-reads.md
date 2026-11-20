---
"tt-project": patch
---

`tt-project`: the daemon's idle tick keeps its spend reads (ledger rows, plan readings, ended runs, each provider's latest run account) across ticks until their own table is written, by any connection, counted by new per-table write triggers. Writes to other tables no longer throw them away, so a project with weeks of history spends about 4-5x less CPU per idle tick. New spend is still read before the next dispatch decision.
