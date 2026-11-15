---
"tt-project": patch
---

`tt-project`:

- every bench table can have a second blind grader: with `bench.second_reviewer` (provider default `chat`, effort default `max`) the daily bench, the replay and a head-to-head run grade the revealed batch again on the same masked items and rubric, and the table shows landed and the mean under both graders and whether the ranking changed
- `ttp bench blind regrade` and `compare` do the same by hand, and `ttp bench blind prep` takes several run folders' cells into one batch; tests
