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
- bench v2 stage 4: one review-fix round (`ttp bench daily --day D --fix-round`, on by default for new days, `bench.fix.enabled`): a cell that did not land first try gets one fix run with the blind reviewer's findings on its first try, the fixed diff is graded again blind (`blind-fix`), and the table shows landed first try and landed after one fix per model with 95% intervals and paired verdicts, plus the fix round's own spend (t8741)
- bench audit: Claude Code records wrap a tool call in an inner message record, which the audit skipped whole; it now reads those calls, and only calls, in every arm format (Claude Code, Codex, mini-swe-agent), never the prompt, prose or tool output (t8741, user msg #3128)
- bench pack: delivery steps (landing, pull requests, push, fetch) are dropped from every arm's context pack the same way, the pack is versioned and content-addressed, rebuilt only at a run boundary, a result set keeps the pack its cells ran on, and each cell row records the pack sha (t8741, user msg #3128)
- smoke list: three bench tests (t8741)
