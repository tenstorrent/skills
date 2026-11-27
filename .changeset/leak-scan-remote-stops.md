---
"tt-project": patch
---

The leak scans in `ttp push` and `ttp checks` skip commits the push remote already has, so merging a commit another developer already pushed is no longer refused; new local commits are still scanned.
