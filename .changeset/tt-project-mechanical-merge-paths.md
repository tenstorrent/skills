---
"tt-project": patch
---

The push queue settles a conflict left only in shared docs (`delivery.mechanical_merge_paths`, default
Markdown docs and changesets) by keeping both sides' lines, and keeps the review approval when the
change's own diff outside them is unchanged, the result holds no conflict marker and the checks pass.
The carry is recorded in the push log and on the review, and the Course check's self-race line skips it.
