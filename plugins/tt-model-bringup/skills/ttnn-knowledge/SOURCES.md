# Sources (for maintainers; not needed by bringup agents)

The topics were written from a review of 158 incidents recorded by five efforts in October and November 2026:
the staged-pipeline bringups of Gemma 4 31B (v5) and Qwen3.8-27B (v1-v4), the freeform Qwen3.8-Flash-Next
bringup, the Gemma 4 31B speedup and Gemma 4 26B A4B performance efforts, and the Ornith-1.5-9B bringup with its
stage 9-12 rerun. Inputs were the stages' AUTODEBUG and AUTOFIX records, work logs, module reports, lead
transcripts and commit times. Each fact cost at least one effort measurable time and is general beyond one model,
or is marked with the architecture feature it concerns.

When a fact here is contradicted by current tt-metal, fix or remove it; facts are not rules.
