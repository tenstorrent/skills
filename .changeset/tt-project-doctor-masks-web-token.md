---
"tt-project": patch
---

`tt-project`: `ttp doctor` no longer prints the web app's full access token. Its web app line shows the address, the token's first 4 characters and `ttp web <project>` for the full link, so the token stays out of worker logs and transcripts. `ttp web`, `ttp new` and `ttp connect` still show the user the full link.
