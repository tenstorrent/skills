---
"tt-model-bringup": minor
---

Add goal 6b, reference accuracy, and the `reference-accuracy` skill: a frozen HF-on-CPU reference corpus
(built in the background from goal 1), a teacher-forced comparison at the current and the highest fitting
precision, implementation divergences fixed before optimisation, and quick/full/long/served tiers that later
stages are gated on. Goal 12 checks every failed task against that reference (verdict model, implementation
or inconclusive) and sends implementation verdicts to the precision-fix procedure. Models with recurrent state
reuse prefixes through session continuation instead of skipping reuse. The KV pool is the largest tested fit;
a fixed or under-half pool without a failed larger test is critical.
