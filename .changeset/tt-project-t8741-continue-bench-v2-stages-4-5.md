---
"tt-project": patch
---

`tt-project`: bench v2 stage 4 (review-fix round, regrade after one fix) + #3128: audit reads Claude Code tool calls in inner message records, delivery steps stripped from every arm's pack at a run boundary, pack sha per cell (t8741).
- bench not-run check: a cell that never launched, made zero model calls or was broken by the server is not_run, never done; calls>0 with an empty diff or empty replay answer fails; not run is its own table count outside pass-rate denominators; each arm's first cell is a canary that must pass before the arm's other cells launch (t8741)
- bench v2 stage 5: fact-match replay grading (ttp bench facts): frozen case file of 2-4 facts per question, two graders (Opus and the open model, high, never max) with the arm hidden, lower verdict counts, disagreements flagged, not-run cells counted apart, empty answers fail (t8741)
