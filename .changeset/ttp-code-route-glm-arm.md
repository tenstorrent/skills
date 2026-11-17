---
"tt-project": patch
---

Code route arm, off by default: `route.code_sonnet.glm_share` with a `glm` arm (provider, model, effort, harness) sends a seeded-coin share of new standard code tasks with no resources, pins, protected class or `conclusion:` line to a chat model run as Claude Code on the chat bridge, the rest to Sonnet; follow-ups keep their lineage's arm and `ttp task show` prints it. A run on the arm can hand back with `needs_opus`: it pushes nothing and the task requeues on Opus with no attempt spent and no provider fault. The daily review shows a per-arm table (review fails, fix rounds, time to land, bounces, reverts, server waits and refusals, cached and uncached tokens, $); a model-free guard holds the share at 0 when the arm lands within one fix round more than 10 points less often than Sonnet or reverts more, and the arm's concurrency cap starts at 4 and moves with refusals.
