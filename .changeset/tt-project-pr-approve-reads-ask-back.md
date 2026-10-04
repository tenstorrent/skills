---
"tt-project": patch
---

`tt-project`:

- inbound messages record the channel they came in on (provenance) and their id there; pr_approve
  only accepts a yes from a channel a run cannot write to, read back from Slack
- pr_approve also reads the ask back from Slack by the ts stored when the daemon posted it; review
  and merge asks always go to Slack
- asks sent before this version was deployed have no stored Slack ts and cannot back a PR approval:
  ask again after upgrading
