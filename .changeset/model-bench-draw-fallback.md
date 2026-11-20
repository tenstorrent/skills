---
"tt-project": patch
---

`tt-project`: the daily model-bench suite no longer stalls when the bench owner project records no landed tasks. When the owner's records give fewer than 12 fair tasks, the draw also tries the other projects on the machine that record landed tasks, most landed first, each with its own repo; `model_bench.sources` (a list of project names) overrides the order. A failed build keeps the old pair and raises one normal observation a day in the owner project, plus a note to each project that sets `model_bench.reference`, instead of only a log line.
