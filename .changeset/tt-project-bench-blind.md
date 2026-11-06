---
"tt-project": patch
---

`tt-project`: `python3 -m ttp.benchblind` adds blind review to model comparisons. Each task's arm diffs are anonymised: model, provider, effort, run ids, timings and transcript references are masked, and a batch where one still shows is refused. The items are shuffled with a sealed seed and scored on a fixed rubric by a decision-only reviewer call. The arm labels are revealed only once every item is scored, and are stored with the seed for audit. No score is taken after the reveal.
