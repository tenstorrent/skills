---
"tt-project": patch
---

`tt-project`: `ttp gh-queue run` reads stdin only for a call that reads it (a body or bundle on `-`), so an open pipe never stalls it; a held read's answer is handed out once, as the queue message and worker prompt now say. The PR sweep's four accepted gaps are documented, with a regression test that reads detailsUrl through the real batched query.
