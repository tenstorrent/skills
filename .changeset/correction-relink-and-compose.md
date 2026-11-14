---
"tt-project": patch
---

`tt-project`: `ttp task <project> relink-fix <correction> <task> --why "..."` re-points a correction's fix to any existing task, done or not, recording the change and its reason. A recurring-mistake count skips a later correction made before the earlier correction's fix landed (landing time read from the push records); while that fix has not landed, the later correction does not count either. The daily Terminal-Bench step finds a usable docker compose plugin itself, from the `model_bench.terminalbench.compose` setting or docker's usual cli-plugins folders, linking it into a docker config folder the bench owns; with none, it stops before any trial with one message naming what is missing.
