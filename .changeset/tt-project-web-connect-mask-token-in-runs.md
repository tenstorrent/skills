---
"tt-project": patch
---

`tt-project`: in a worker run (`TTP_RUN_DIR` set), `ttp web`, `ttp new` and `ttp connect` print the web app's address and only the first 4 characters of its access token, with a note that the full link is shown only outside worker runs, so the token stays out of worker transcripts. In the user's own shell they still print the full link.
