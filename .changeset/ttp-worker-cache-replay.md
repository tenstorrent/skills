---
"tt-project": patch
---

tt-project: workers and reviews keep a 5-minute prompt cache, set explicitly, with the evidence written down. Claude
Code 2.1.285 reads `CLAUDE_CODE_PROMPT_CACHE_TTL` and, unset, picks 1 hour on a Claude subscription. A replay of a
day of live runs found 1 hour cost 11% more for workers and 27% more for reviews, and no rule chosen at a run's
start saved more than 2%.
