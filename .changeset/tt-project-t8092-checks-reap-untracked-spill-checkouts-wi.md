---
"tt-project": patch
---

`tt-project`:

- ttp checks: orphaned remote check runs are stopped automatically
- tt_smoke: the orphan-check sweep test is in the smoke list so its case file has a smoke test
- remotecheck: after the rebase on the mirror copy, the cleanup removes the mirror ref first, then reaps the checkout's groups
