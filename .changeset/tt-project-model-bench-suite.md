---
"tt-project": patch
---

`tt-project`:

- `ttp model-bench suite`: a daily cheat-resistant bench of the served chat models at their class efforts (top four and new models daily, weak or parked ones weekly, models named in `model-bench/requests.jsonl` at once). It runs 12 replay and 4 injection cells by a date-seeded draw, each isolated (held-out tests copied in after the run, hidden paths audited in the transcript, a fresh venv per cell), and 40 recorded babysit decisions with redacted states and planted-instruction copies. It writes one row per model and class to `model-bench/<date>.jsonl`. Fixed checks grade it; no model grades. It runs niced and needs the claude CLI, so never on spill hosts; tests
