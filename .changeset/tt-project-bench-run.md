---
"tt-project": patch
---

`tt-project`: `ttp bench run --bench <dir> --out <dir>` runs a head-to-head of two or more model arms (provider, model and effort, from `bench.arms` or `--arm`) on a `ttp bench prep` bench. Arm order per task is drawn from a recorded seed; every cell gets a fresh base workspace, the same prompt and the same wall-clock, stall and budget limits through the daemon's run supervisor. A cell whose workspace could show the landed change is refused, a transcript that reaches for the bench, the landed commit or history fails, and the held-out tests grade the rest. Each cell records cost, tokens, wall time, hand-off status, pass or fail and its transcript hash in `results.jsonl`. `--dry-run` uses the fake agent, with no model spend.
