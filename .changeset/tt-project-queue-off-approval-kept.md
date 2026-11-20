---
"tt-project": patch
---

`tt-project`:

- push queue off: a review's approval that names a commit in any form (a list, one entry, "branch at sha", the branch alone, a sha in prose) is never dropped as ignored. The review runs again to push the head it approved with `ttp push`; a value naming no commit, or only commits already on the push branch, is still ignored. Tests.
