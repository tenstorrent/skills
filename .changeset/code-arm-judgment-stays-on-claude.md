---
"tt-project": patch
---

Code route arm: a code spec that leaves a design or policy choice open (it asks to weigh options, names a trade-off, says no data settles it, or asks to decide between designs) stays on Claude model-free (stay reason `judgment`), and the arm's hand-back prompt now asks the run, before its first edit, whether the spec leaves a choice, an unseen root cause or a person to it, and to hand back rather than decide. A dry-run arm run had decided a batching policy itself instead of handing it back.
