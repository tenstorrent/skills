---
"tt-project": patch
---

`tt-project`: the chat endpoint's served-model list is checked every minute (was hourly; `providers.chat.models_every_s`), also while only run classes route to `chat`, so a model that goes away is swapped for the next ranked one within about a minute. A served model ranked above a route's own model (`providers.chat.model_ranking`) is tried at once on light starts and runs at every tier after 3 good runs; its own faults (malformed answer, timeout, no hand-off) during those runs, or in over a fifth of 8+ runs later, park it for a week with one low alert, after which it is tried again. The untrusted-input guard's credential-name check again catches names such as `authtoken` and `mytoken.txt`, while tokenizer files stay readable.
