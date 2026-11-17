---
"tt-project": patch
---

`tt-project`:

- bench v2 stage 1: context pack of the original worker (prompt, cited tasks and messages, review findings, live-read snapshots; sha256-pinned, one per task, the same for every arm) and an unlabeled original-change arm in every blind batch; live-only and invalid tasks leave the code bench, a task whose original fails is dropped, both listed with why (t8733)
- smoke list: one fast benchpack test (t8733)
- benchpack: packs and the original arm's diff are written durably (t8733)
- bench v2 stage 2: repeat runs (N per arm on a seeded subset) and repeat grades (M per diff), flip rates run-to-run and grade-to-grade, pass rates with 95% Wilson intervals, paired differences on shared tasks; a verdict only when the interval excludes zero (t8733)
