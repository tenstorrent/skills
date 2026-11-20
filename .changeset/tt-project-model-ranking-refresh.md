---
"tt-project": patch
---

`tt-project`: the chat provider's `providers.chat.model_ranking` can be set account-wide (`ttp config --account`), so every project on the machine that uses `chat` reads it; a project's own value wins. A model-free refresher keeps it current from the Artificial Analysis Intelligence Index, weekly (`providers.chat.ranking_every_s`) and whenever the /models check lists a new model, with nothing to turn on. It maps served ids to the index by normalised name, ranks unmatched ids last, logs each change to `~/.tt-project/model-ranking.jsonl` with one low feed line, keeps the last ranking on a failed fetch with one observation per outage, and raises one user-only item while no API key is set (`ARTIFICIAL_ANALYSIS_API_KEY` or `providers.chat.ranking_key_file`).
