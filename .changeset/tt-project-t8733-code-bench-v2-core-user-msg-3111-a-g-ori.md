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
- bench audit: Claude Code records wrap a tool call in an inner message record, which the audit skipped whole; it now reads those calls, and only calls, in every arm format (Claude Code, Codex, mini-swe-agent), never the prompt, prose or tool output (t8741)
- bench pack: delivery steps (landing, pull requests, push, fetch) are dropped from every arm's context pack the same way, the pack is versioned and content-addressed, rebuilt only at a run boundary, a result set keeps the pack its cells ran on, and each cell row records the pack sha (t8741)
- smoke list: three bench tests (t8741)
- bench not-run check, every bench that grades cells (v2 daily, harness A/B, replay, optimization arms): a cell that never launched, made zero model calls, or that the model server broke with no diff is `not_run`, never done or failed, shown as its own count per arm outside every pass-rate denominator, never reviewed and rerun on a resume; a model that worked and left no diff (or an empty replay answer) fails as before, the same for every arm; each arm's first cell of a run must pass the check before the arm's other cells launch, and an arm whose first cell was not run is stopped with a clear error
- smoke list: two not-run tests (t8741)
- bench v2 stage 5, fact-match replay grading (`ttp bench facts`, benchfacts.py): the replay's case file holds 2-4 checkable facts per question, frozen by sha256 (`--freeze`) before any model grades it; two graders (Opus and the open model, high effort, never above) judge each answer with the arm hidden; match/partial/miss/contradicted per fact, the lower verdict counts and every disagreement is flagged; a not-run cell is its own count, an empty answer from a model that ran fails; per-arm fact-match score, hits with Wilson 95% and the paired verdict (t8741)
- smoke list: three fact-grading tests (t8741)
