---
"tt-project": patch
---

The chat endpoint's /models list is polled once per machine and endpoint for every project on it: every 30 seconds while any project routes work to chat, else every 5 minutes (account keys `providers.chat.served_every_s` and `providers.chat.served_idle_s`). One daemon holds the poll lease and another takes over 90 seconds after it stops. The shared list under `~/.tt-project/served/` records when each model was first and last listed, and an events file records each model that appears or goes away. A list older than 3 poll intervals counts as unknown, so a model that goes away gets its ranked stand-in within a minute and no swap rests on an old list. The key and URL are never written.
