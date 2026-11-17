---
"tt-project": patch
---

`tt-project`:

- bench v2 stage 1: context pack of the original worker (prompt, cited tasks and messages, review findings, live-read snapshots; sha256-pinned, one per task, the same for every arm) and an unlabeled original-change arm in every blind batch; live-only and invalid tasks leave the code bench, a task whose original fails is dropped, both listed with why (t8733)
- smoke list: one fast benchpack test (t8733)
- benchpack: packs and the original arm's diff are written durably (t8733)
- bench v2 stage 2: repeat runs (N per arm on a seeded subset) and repeat grades (M per diff), flip rates run-to-run and grade-to-grade, pass rates with 95% Wilson intervals, paired differences on shared tasks; a verdict only when the interval excludes zero (t8733)
- bench v2 stage f: no default daily cap (0 = uncapped, a configurable runaway guard replaces it, off for ordered runs), per-provider cell concurrency (bench.cells.claude / .glm) and spend per arm in every table (t8733)
- bench v2 stage 3: fail-to-pass tests written by a model that sees no arm's diff; a written test is kept only when it fails on the pre-change tree and passes on the change, run on git archives of both trees, a broken module is a fault never a kept test (t8733)
