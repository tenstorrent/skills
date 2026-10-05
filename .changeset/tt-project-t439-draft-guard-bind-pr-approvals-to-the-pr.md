---
"tt-project": patch
---

`tt-project`:

- harden the PR draft guard: clear-yes approvals spent once, spec checks, checks before draft PRs, PR findings as work
- tests for the hardened draft guard (clear yes, spent approvals, spec checks, checks before draft PRs, PR findings)
- a clear yes no longer counts 'please' or a lone 'y' inside a sentence
- bind PR approvals to the PR's head commit
- disk guard test keeps a wide size margin over the copied harness
- pr-watch cancelled-task test stubs the findings query too
- draft-guard tests follow the Slack-provenance rules (head readings, approving channels)
