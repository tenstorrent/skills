---
"tt-project": patch
---

`tt-project`: `ttp push` no longer refuses a branch as based outside the flow when it already
descends from the push target's tip, even if it merged in another branch (such as main) that it has
fewer commits over. Such a branch is rebased onto the push target as usual, keeping what it merged.
