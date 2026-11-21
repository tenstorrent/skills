---
"tt-project": patch
---

Course check's "over its estimate" line measures a long job (one with listed cells) by its per-cell
time times its cells, as `ttp eta` does, instead of the kind's median run, so a long job still within
its estimate is no longer flagged. Both share one estimate rule, history is read once per digest, and
the line says "ended runs" rather than "done runs".
