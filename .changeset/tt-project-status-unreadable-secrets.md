---
"tt-project": patch
---

`tt-project`: `ttp status` and `ttp status --json` no longer crash where the secrets store cannot be read, as in a worker sandbox. They skip it: the text says `secrets store: unreadable, skipped`, the JSON (and the web state) carries `secrets: {"readable": false, "path", "error"}`, and the Slack line reads "unknown here". A command that needs a secret still fails, now with a one-line `cannot read the secrets store` message instead of a traceback.
