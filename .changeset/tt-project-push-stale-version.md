---
"tt-project": patch
---

`tt-project`: `ttp push` refuses (exit 4) a rebased change that edits a plugin under
`plugins/<name>/` while a manifest still has the version already on the target branch. Two batches
that both bumped to the same version rebase onto each other cleanly, and installs that only upgrade
to a strictly newer version would skip the second one. The review prompt tells the reviewer to bump
past it and rerun.
