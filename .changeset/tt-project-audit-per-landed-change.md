---
"tt-project": patch
---

`tt-project`:

- the daily review's self-efficiency audit (`ttp audit`) shows $ per landed change over the last 24 h and 7 d, as `ttp stats --landing` does: booked spend over the commits landed on the push branch (merges and version bumps left out) and the subagents those runs started, on a `$ per landed change` line and as `per_landed` in `ttp audit --json`. An unreadable push branch shows the count as unknown, never zero; a day with no spend and no landings adds no line.
