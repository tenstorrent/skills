---
"tt-project": patch
---

`tt-project`: `ttp model-bench terminalbench --reference` runs the fixed reference models (default Opus 5.5 and Sonnet 5.5, medium effort) once over the full Terminal-Bench task list through a configured Anthropic-capable endpoint, model-free and detached, and books one reference row per model once. The paced cycle and its schedule never run it. `ttp model-bench scores` and `show` list the reference rows beside the ranking, marked as reference, or the earlier 20-task subset result, marked as such. With no endpoint saved it prints one line and starts nothing.
