---
"tt-project": patch
---

tt-project: `ttp model-bench`, one shared daily chat model bench per machine. The first daemon after 02:00 local time runs it under a machine-wide lock, and every project reads that result. It sends six fixed-graded prompts, including one prompt-injection check, to the top served ranked models and to new models, and once a week to weak or parked models. A weak score keeps a model parked; a pass frees a parked model at once. With no working endpoint key it calls no model and posts one low alert per outage.
