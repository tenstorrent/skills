---
"tt-project": patch
---

`tt-project`: the retired-rule echo scan (`ttp echoes`, the digest's Retired-rule echoes section) no longer flags a phrase of a superseded memory entry that the live entry superseding it still carries, directly or down a chain of later supersedes. This holds whatever the retirement's source or retired text hash, so an acknowledged false positive does not come back when the retirement is recorded again. A phrase found only in the retired entry, or one the live superseder dropped, is still flagged.
