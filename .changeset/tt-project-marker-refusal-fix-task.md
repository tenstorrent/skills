---
"tt-project": patch
---

`tt-project`: when the push queue refuses a plain review's approval because a commit adds a git conflict marker, the review fails as `marker` (no rerun, no area-cap count) and one fix task on the code branch names the commits that carry the marker and asks for a squash or in-place fix, followed by the usual re-review. An open fix on that branch is reused, never duplicated. A conflict-fix review still runs again as before.
