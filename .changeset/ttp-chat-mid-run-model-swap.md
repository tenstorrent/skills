---
"tt-project": patch
---

A chat run whose model goes away mid-run moves to the highest-ranked served model in the same session: on a refusal, or 3 errors in a row while the /models check no longer lists it. The run note keeps each swap (model_rank.mid_run) with one low alert; a required pin never swaps. Babysit asks the next ranked served model once before it falls back.
