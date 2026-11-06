---
"tt-project": patch
---

`tt-project`: inside a run, the harness's gh and the worker hook also refuse GitHub writes to branch protection, tag protection and rulesets, PR branch updates (`gh pr update-branch`, its REST route and GraphQL mutation), and every `gh release` write (edit, delete, upload, delete-asset as well as create), matching the REST release routes already refused; reads of the same endpoints still pass. A session coordinator's file tools also refuse a non-public `~/.ssh` file not listed at launch (a key made later, or one reached by a symlink).
